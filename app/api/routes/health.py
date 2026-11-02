from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile

import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes.accounts import session_for
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.provider_runtime.service import ProviderRuntimeService
from app.services.search.providers import ProviderRegistry, normalize_searxng_instance
from app.services.search.searxng_embedded import source_available

router = APIRouter(prefix="/health", tags=["health"])

#: These routes answer without a session, and any web page can make a browser
#: send them a GET (an ``<img>`` pointed at 127.0.0.1 is enough). So nothing an
#: anonymous caller can trigger may cost more than a cached answer: the network
#: probes below are remembered, and the full readiness checks -- a real web search
#: and a model round trip -- are only ever run for a signed-in caller.
PROBE_TTL_SECONDS = 10.0
READINESS_TTL_SECONDS = 30.0
ANONYMOUS_READINESS_TTL_SECONDS = 5.0

_probe_lock = threading.Lock()
_probe_cache: dict[str, tuple[float, bool]] = {}
#: Held while the full checks run, so a burst of signed-in callers waits for one
#: run instead of each starting its own search and model call.
_readiness_lock = threading.Lock()
_readiness_cache: tuple[float, dict[str, dict[str, object]]] | None = None
_storage_cache: tuple[float, dict[str, object]] | None = None


def reset_caches() -> None:
    """Forget every remembered probe and readiness result."""

    global _readiness_cache, _storage_cache
    with _probe_lock:
        _probe_cache.clear()
    with _readiness_lock:
        _readiness_cache = None
        _storage_cache = None


def _cached_probe(key: str, probe: Callable[[], bool]) -> bool:
    now = time.monotonic()
    with _probe_lock:
        hit = _probe_cache.get(key)
        if hit is not None and now - hit[0] < PROBE_TTL_SECONDS:
            return hit[1]
    value = probe()
    with _probe_lock:
        _probe_cache[key] = (time.monotonic(), value)
    return value


def _ollama_available(url: str) -> bool:
    def probe() -> bool:
        try:
            response = requests.get(f"{url.rstrip('/')}/api/tags", timeout=2)
            return response.ok
        except requests.RequestException:
            return False

    return _cached_probe(f"ollama:{url}", probe)


def _search_available(provider: str, searxng_url: str) -> bool:
    if provider == "disabled":
        return False
    if provider == "searxng":
        # The built-in instance runs inside this process, so there is nothing to
        # probe over the network -- only whether its source tree is in place.
        # Booting it here would make a liveness-adjacent check pay a five-second
        # import, so this deliberately stays a file check.
        return source_available()
    if provider != "external_searxng":
        return True

    def probe() -> bool:
        try:
            url = normalize_searxng_instance(searxng_url)
            response = requests.get(url, timeout=2)
            return response.status_code < 500
        except (ValueError, requests.RequestException):
            return False

    return _cached_probe(f"searxng:{searxng_url}", probe)


@router.get("")
def health(request: Request) -> dict[str, object]:
    """Basic availability. Reachable without a session, so it says less to one.

    ``data_dir`` is an absolute path into the user's filesystem and a precise
    pointer at every profile database on the machine. It is still reported -- it is
    the field used to confirm which installation answered after a restart -- but
    only to a caller that has signed in. An anonymous probe gets availability only.
    """

    settings = get_settings()
    body: dict[str, object] = {
        "status": "ok",
        "search_provider": settings.web_search_provider,
        "search_available": _search_available(
            settings.web_search_provider, settings.searxng_instance
        ),
        "ollama_available": _ollama_available(settings.ollama_url),
        "external_agents_enabled": _external_agents_enabled(),
        "external_agents": _external_agents(),
    }
    if session_for(request) is not None:
        data_dir = settings.data_dir
        if not data_dir and settings.database_url.startswith("sqlite:///"):
            data_dir = str(
                Path(settings.database_url.removeprefix("sqlite:///")).resolve().parent
            )
        body["data_dir"] = data_dir or str(Path(".").resolve())
    return body


def _external_agents_enabled() -> bool:
    """The profile's choice, falling back to the deployment setting.

    Health must never fail on a preference read, so a profile database that is
    not there yet answers with the setting rather than raising.
    """

    try:
        from app.services import chat_prefs

        return chat_prefs.external_agents_enabled()
    except Exception:  # pragma: no cover - health must never fail on a read
        return bool(get_settings().external_agents_enabled)


def _external_agents() -> list[dict[str, object]]:
    """Which external executors are usable, if the feature is on at all.

    Cached inside ``detect``, so this stays cheap enough for a health endpoint;
    when the feature is disabled it does not probe anything.
    """

    if not _external_agents_enabled():
        return []
    try:
        from app.services.external_agents import detect

        return [
            {"id": row["id"], "available": row["available"], "reason": row.get("reason")}
            for row in detect.statuses()
        ]
    except Exception:  # pragma: no cover - health must never fail on a probe
        return []


@router.get("/live")
def liveness() -> dict[str, str]:
    """Process-only probe that does not depend on external services."""

    return {"status": "alive"}


def _storage_root() -> Path:
    settings = get_settings()
    if settings.data_dir:
        return Path(settings.data_dir).expanduser().resolve()
    if settings.database_url.startswith("sqlite:///"):
        database_path = Path(settings.database_url.removeprefix("sqlite:///")).expanduser()
        return database_path.resolve().parent
    return Path(".").resolve()


def _storage_check() -> dict[str, object]:
    """Writable storage and a live SQLite connection: local, cheap, no network."""

    try:
        root = _storage_root()
        root.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix=".neo-ready-", dir=root):
            pass
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1")).scalar_one()
        finally:
            db.close()
        return {"ok": True, "path": str(root)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _anonymous_storage_ok() -> bool:
    """The storage check, remembered briefly so a flood of probes is not a flood of writes."""

    global _storage_cache
    with _readiness_lock:
        now = time.monotonic()
        if _storage_cache is None or now - _storage_cache[0] >= ANONYMOUS_READINESS_TTL_SECONDS:
            _storage_cache = (time.monotonic(), _storage_check())
        return _storage_cache[1].get("ok") is True


def _cached_readiness_checks() -> dict[str, dict[str, object]]:
    global _readiness_cache
    with _readiness_lock:
        now = time.monotonic()
        if _readiness_cache is None or now - _readiness_cache[0] >= READINESS_TTL_SECONDS:
            _readiness_cache = (time.monotonic(), _readiness_checks())
        return _readiness_cache[1]


def _readiness_checks() -> dict[str, dict[str, object]]:
    settings = get_settings()
    checks: dict[str, dict[str, object]] = {"storage": _storage_check()}

    try:
        model = ProviderRuntimeService().health_check(route_name="chat")
        checks["model"] = {
            "ok": model.get("status") == "healthy",
            "status": model.get("status"),
            "provider": model.get("provider_name"),
            "model": model.get("model_name"),
            "error": model.get("error_message"),
        }
    except Exception as exc:
        checks["model"] = {"ok": False, "error": str(exc)}

    try:
        provider_name = settings.web_search_provider
        response = (
            ProviderRegistry()
            .provider(provider_name)
            .search(
                "Neo assistant readiness check",
                max_results=1,
            )
        )
        checks["search"] = {
            "ok": provider_name != "disabled" and response.error is None,
            "provider": provider_name,
            "result_count": len(response.results),
            "error": response.error,
        }
    except Exception as exc:
        checks["search"] = {
            "ok": False,
            "provider": settings.web_search_provider,
            "error": str(exc),
        }

    try:
        from app.services.integrations import vault

        key = vault.key_status()
        checks["connector_key"] = {"ok": key["state"] == "ok", **key}
    except Exception as exc:
        checks["connector_key"] = {"ok": False, "state": "error", "detail": str(exc)}

    # Voice is an optional extra, and readiness is all-or-nothing: the caller below
    # requires every check to be ok. So "not installed" and "switched off" both report
    # ok, with the reason alongside -- reporting them as failures would take readiness
    # down on every existing deployment, none of which asked for speech recognition.
    # Only a voice setup that is switched on and genuinely broken is a failure.
    try:
        from app.services.voice.service import VoiceService

        report = VoiceService().availability()
        checks["voice"] = {
            "ok": report.available or report.reason != "engine_error",
            "enabled": bool(settings.voice_enabled),
            "available": report.available,
            "reason": report.reason,
        }
    except Exception as exc:
        checks["voice"] = {
            "ok": True,
            "enabled": False,
            "reason": "engine_error",
            "error": str(exc),
        }

    return checks


@router.get("/ready")
def readiness(request: Request) -> JSONResponse:
    """Dependency-aware probe used before accepting production traffic.

    Anonymous callers learn only whether this process can serve: storage and the
    database, nothing that leaves the machine, and no path or error text. The full
    report -- model, search, voice, the storage path and every error -- is for a
    signed-in caller, and even then it is computed at most once per TTL.
    """

    if session_for(request) is None:
        ready = _anonymous_storage_ok()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready"},
        )
    checks = _cached_readiness_checks()
    ready = all(check.get("ok") is True for check in checks.values())
    body: dict[str, object] = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
    }
    # Advisory, and not part of readiness: a stray store from an older launch does
    # not stop Neo working, and refusing traffic over it would be wrong. It is
    # reported because nobody would otherwise know the data was there.
    try:
        from app.services.isolation import hygiene

        report = hygiene.report()
        if report.get("stray_stores"):
            body["storage_notices"] = report
    except Exception:  # pragma: no cover - a notice must never fail a probe
        pass
    return JSONResponse(status_code=200 if ready else 503, content=body)
