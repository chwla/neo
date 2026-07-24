from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.provider_runtime.service import ProviderRuntimeService
from app.services.search.providers import ProviderRegistry, normalize_searxng_instance
from app.services.tools.executor import ToolsService
from app.services.tools.mcp import health_check as connector_health_check
from app.services.tools.vault import master_key

router = APIRouter(prefix="/health", tags=["health"])


def _ollama_available(url: str) -> bool:
    try:
        response = requests.get(f"{url.rstrip('/')}/api/tags", timeout=2)
        return response.ok
    except requests.RequestException:
        return False


def _search_available(provider: str, searxng_url: str) -> bool:
    if provider == "disabled":
        return False
    if provider not in {"external_searxng", "searxng"}:
        return True
    try:
        url = normalize_searxng_instance(searxng_url)
        response = requests.get(url, timeout=2)
        return response.status_code < 500
    except (ValueError, requests.RequestException):
        return False


@router.get("")
def health() -> dict[str, object]:
    settings = get_settings()
    data_dir = settings.data_dir
    if not data_dir and settings.database_url.startswith("sqlite:///"):
        data_dir = str(Path(settings.database_url.removeprefix("sqlite:///")).resolve().parent)
    return {
        "status": "ok",
        "data_dir": data_dir or str(Path(".").resolve()),
        "search_provider": settings.web_search_provider,
        "search_available": _search_available(
            settings.web_search_provider, settings.searxng_instance
        ),
        "ollama_available": _ollama_available(settings.ollama_url),
    }


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


def _readiness_checks() -> dict[str, dict[str, object]]:
    settings = get_settings()
    checks: dict[str, dict[str, object]] = {}

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
        checks["storage"] = {"ok": True, "path": str(root)}
    except Exception as exc:
        checks["storage"] = {"ok": False, "error": str(exc)}

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
        key = master_key()
        checks["vault"] = {"ok": len(key) == 32, "encrypted": True}
    except Exception as exc:
        checks["vault"] = {"ok": False, "error": str(exc)}

    required_connectors: list[dict[str, object]] = []
    try:
        for server in ToolsService().list_servers(include_disabled=False):
            metadata = server.metadata or {}
            if not (metadata.get("required") or metadata.get("required_for_readiness")):
                continue
            result = connector_health_check(server.model_dump())
            required_connectors.append(
                {
                    "id": server.id,
                    "name": server.name,
                    **result,
                }
            )
        checks["connectors"] = {
            "ok": all(item.get("ok") is True for item in required_connectors),
            "required": required_connectors,
        }
    except Exception as exc:
        checks["connectors"] = {"ok": False, "error": str(exc)}
    return checks


@router.get("/ready")
def readiness() -> JSONResponse:
    """Dependency-aware probe used before accepting production traffic."""

    checks = _readiness_checks()
    ready = all(check.get("ok") is True for check in checks.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not_ready", "checks": checks},
    )
