"""Per-profile web-search settings.

The provider, the SearXNG instance and the Tavily key used to be written straight
into the process-wide ``Settings`` object by the search configuration endpoint, so
whatever one profile chose applied to every account on the machine -- a guest
session included. They live in the profile's own preferences table now (the one
``chat_prefs`` already keeps) and are overlaid onto the settings for each request;
the environment stays the deployment default for a profile that has never chosen.

Reads are cached per profile database, because ``get_settings()`` is called many
times per request and a SQLite round trip each time would be felt everywhere.
"""

from __future__ import annotations

import threading

from app.core.config import active_profile_database_url
from app.services import chat_prefs

PROVIDER_KEY = "web_search_provider"
SEARXNG_INSTANCE_KEY = "web_search_searxng_instance"
TAVILY_KEY = "web_search_tavily_api_key"

_KEYS = (PROVIDER_KEY, SEARXNG_INSTANCE_KEY, TAVILY_KEY)

_cache: dict[str, dict[str, str]] = {}
_lock = threading.Lock()


def _current_database() -> str | None:
    return active_profile_database_url.get()


def stored() -> dict[str, str]:
    """The current profile's stored search choices (only the keys that are set)."""

    database = _current_database()
    if database is None:
        return {}
    cached = _cache.get(database)
    if cached is not None:
        return cached
    values: dict[str, str] = {}
    for key in _KEYS:
        try:
            value = chat_prefs.get_preference(key)
        except Exception:  # noqa: BLE001 - a preference that cannot be read is unset
            value = None
        if value is not None:
            values[key] = value
    with _lock:
        _cache[database] = values
    return values


def overlay() -> dict[str, object]:
    """Settings fields the current profile overrides, for ``get_settings``."""

    values = stored()
    updates: dict[str, object] = {}
    if PROVIDER_KEY in values:
        provider = values[PROVIDER_KEY]
        updates["web_search_provider"] = provider
        updates["web_search_enabled"] = provider != "disabled"
    if SEARXNG_INSTANCE_KEY in values:
        updates["searxng_instance"] = values[SEARXNG_INSTANCE_KEY]
    if TAVILY_KEY in values:
        updates["tavily_api_key"] = values[TAVILY_KEY] or None
    return updates


def save(*, provider: str, searxng_instance: str, tavily_api_key: str | None) -> None:
    """Record the current profile's choices, replacing what it had."""

    chat_prefs.set_preference(PROVIDER_KEY, provider)
    chat_prefs.set_preference(SEARXNG_INSTANCE_KEY, searxng_instance)
    chat_prefs.set_preference(TAVILY_KEY, tavily_api_key or "")
    forget()


def forget(database: str | None = None) -> None:
    """Drop the cache for one database (the current one by default), or for all."""

    with _lock:
        if database is None:
            database = _current_database()
        if database is None:
            _cache.clear()
        else:
            _cache.pop(database, None)
