from __future__ import annotations

from pathlib import Path

import requests
from fastapi import APIRouter

from app.core.config import get_settings
from app.services.search.providers import normalize_searxng_instance

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
