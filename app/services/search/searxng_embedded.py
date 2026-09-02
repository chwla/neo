"""SearXNG running inside Neo's own process, with no container and no port.

``searx.webapp:app`` is an ordinary Flask WSGI app -- the official SearXNG
image's entrypoint is literally ``exec granian searx.webapp:app`` -- so there is
nothing a second container provides that an import does not. This module holds
that import and drives the app object through a WSGI client, which is the same
code path granian would drive, minus the socket.

What that buys: ``docker compose up`` starts one service, a locally run
``uvicorn app.main:app`` has working SearXNG search without anyone remembering to
start a container first, and the compose file needs no bridge network, no
healthcheck, and no ``depends_on`` gate.

The source tree is fetched by ``scripts/setup_searxng.py`` (or baked into the
image by the ``searxng-src`` Dockerfile stage) and is deliberately optional. When
it is absent every path here returns a ``WebSearchResponse`` carrying an error
rather than raising, so ``ProviderRegistry.chain()`` falls through to DuckDuckGo
and Bing exactly as it did when the SearXNG container was down.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.search.providers import WebSearchProvider, _results_response
from app.services.search.types import WebSearchResponse

logger = logging.getLogger(__name__)

# Import, and the failure that import produced, are both memoized: booting
# SearXNG costs a few seconds, and a broken install must not pay that on every
# query. _BOOT_LOCK guards the first call so concurrent searches during warm-up
# import once rather than racing.
_BOOT_LOCK = threading.Lock()
_APP: Any | None = None
_BOOT_ERROR: str | None = None
_BOOTED = False


# Both defaults are repo-relative, and the server is not always started from the
# repo root -- systemd units and `uvicorn` invocations from elsewhere are normal.
# Anchoring relative values here rather than on the process cwd keeps those from
# silently resolving to a directory that does not exist.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _resolve(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = _REPO_ROOT / path
    return path.resolve()


def _source_dir() -> Path:
    return _resolve(get_settings().searxng_source_dir)


def _settings_path() -> Path:
    return _resolve(get_settings().searxng_settings_path)


def source_available() -> bool:
    """Whether a fetched SearXNG tree is present. Cheap; does not import it."""

    return (_source_dir() / "searx" / "webapp.py").is_file()


def _quiet_searx_logging() -> None:
    """Keep SearXNG's per-engine chatter out of Neo's logs.

    A healthy search still logs a rate-limit, a CAPTCHA, an engine timeout or a
    failed engine registration for several engines every time -- which is not an
    incident, it is the condition SearXNG exists to paper over by querying many
    engines at once. SearXNG logs those at ERROR, not WARNING, so silencing them
    means going above ERROR; anything less leaves Neo's log reporting routine
    upstream flakiness as though Neo were broken.

    Nothing diagnostic is lost. A failure that actually stops search from working
    reaches Neo as an exception out of the import in :func:`_boot`, or as an error
    on the response, and both are reported from here under Neo's own logger.
    """

    logging.getLogger("searx").setLevel(logging.CRITICAL)


def _boot() -> Any | None:
    """Import ``searx.webapp`` and return its Flask app, or None with the error set."""

    global _APP, _BOOT_ERROR, _BOOTED  # noqa: PLW0603

    with _BOOT_LOCK:
        if _BOOTED:
            return _APP
        _BOOTED = True

        source = _source_dir()
        settings_file = _settings_path()

        if not source_available():
            _BOOT_ERROR = (
                f"SearXNG source not found at {source}. "
                "Run scripts/setup_searxng.py, or select a different search provider."
            )
            logger.info("Embedded SearXNG unavailable: %s", _BOOT_ERROR)
            return None
        if not settings_file.is_file():
            _BOOT_ERROR = f"SearXNG settings file not found at {settings_file}."
            logger.warning("Embedded SearXNG unavailable: %s", _BOOT_ERROR)
            return None

        # searx/__init__.py calls init_settings() at import time, so these have
        # to be in the environment before anything imports searx -- setting them
        # afterwards is silently too late.
        os.environ["SEARXNG_SETTINGS_PATH"] = str(settings_file)
        os.environ["SEARXNG_DISABLE_ETC_SETTINGS"] = "1"
        _quiet_searx_logging()

        # Extending sys.path here rather than PYTHONPATH keeps the searx package
        # invisible to the rest of Neo's import graph until something actually
        # asks for a search.
        if str(source) not in sys.path:
            sys.path.insert(0, str(source))

        try:
            from searx.webapp import app  # noqa: PLC0415 -- import *is* the boot
        except SystemExit as exc:
            # webapp.init() calls sys.exit(1) when server.secret_key is still the
            # stock "ultrasecretkey". SystemExit is not an Exception subclass, so
            # it has to be caught by name or it takes the process down with it.
            _BOOT_ERROR = (
                f"SearXNG refused to start (exit {exc.code}); "
                f"check server.secret_key in {settings_file}."
            )
            logger.error("Embedded SearXNG failed to boot: %s", _BOOT_ERROR)
            return None
        except Exception as exc:  # noqa: BLE001 -- a broken tree must not 500 a search
            _BOOT_ERROR = f"SearXNG failed to load: {exc}"
            logger.exception("Embedded SearXNG failed to boot")
            return None

        _quiet_searx_logging()  # webapp's own init reconfigures logging
        _APP = app
        logger.info("Embedded SearXNG ready (source=%s)", source)
        return _APP


def warm_up() -> None:
    """Boot SearXNG ahead of the first query. Best effort; never raises.

    Called from app startup in a background thread. The import costs roughly five
    seconds cold, which is a poor thing to charge the first user search -- or the
    first ``/api/health/ready`` probe, which runs a real query.
    """

    try:
        _boot()
    except Exception:  # noqa: BLE001 -- warm-up must never break startup
        logger.exception("Embedded SearXNG warm-up failed")


def reset_for_tests() -> None:
    """Drop the memoized app so a test can change settings and re-boot."""

    global _APP, _BOOT_ERROR, _BOOTED  # noqa: PLW0603

    with _BOOT_LOCK:
        _APP = None
        _BOOT_ERROR = None
        _BOOTED = False


class EmbeddedSearXNGProvider(WebSearchProvider):
    name = "searxng"

    def __init__(self, max_results: int | None = None) -> None:
        del max_results  # accepted for symmetry with the other providers

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
    ) -> WebSearchResponse:
        app = _boot()
        if app is None:
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error=_BOOT_ERROR or "Embedded SearXNG is unavailable.",
            )

        params: dict[str, object] = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": 1,
        }
        if time_filter in {"day", "week", "month", "year"}:
            # SearXNG's news engines are too sparse at a one-day window to be
            # worth asking for; a week is the narrowest range that still answers.
            params["time_range"] = "week" if time_filter == "day" else time_filter

        try:
            from werkzeug.test import Client  # noqa: PLC0415 -- ships with flask

            response = Client(app).get(
                "/search",
                query_string=params,
                # SearXNG's trusted-proxies middleware pops REMOTE_ADDR without
                # checking for it, and werkzeug's test client does not set one.
                # Without this the very first request dies on a KeyError.
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
                headers={"X-Forwarded-For": "127.0.0.1"},
            )
        except Exception as exc:  # noqa: BLE001 -- an engine fault is not a Neo fault
            logger.warning("Embedded SearXNG query failed: %s", exc)
            return WebSearchResponse(
                query=query, provider=self.name, error=f"SearXNG search failed: {exc}"
            )

        if response.status_code != 200:
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error=f"SearXNG returned HTTP {response.status_code}.",
            )

        try:
            payload = json.loads(response.get_data())
        except (ValueError, json.JSONDecodeError) as exc:
            return WebSearchResponse(
                query=query, provider=self.name, error=f"SearXNG search failed: {exc}"
            )

        # Same shape the HTTP-backed provider parses; SearXNG's JSON is identical
        # whether it arrives over a socket or straight out of the WSGI callable.
        raw = [
            {
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or item.get("snippet") or "",
                "published_date": item.get("publishedDate") or item.get("published_date"),
            }
            for item in payload.get("results", [])
        ]
        return _results_response(query, self.name, raw, max_results)
