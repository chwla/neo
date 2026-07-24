from __future__ import annotations

from app.api.routes import search
from app.core.config import Settings


def test_selecting_keyless_provider_enables_search(monkeypatch) -> None:
    settings = Settings(web_search_provider="disabled")
    monkeypatch.setattr(search, "get_settings", lambda: settings)

    result = search.update_search_config(search.SearchConfigUpdateRequest(provider="duckduckgo"))

    assert settings.web_search_provider == "duckduckgo"
    assert settings.web_search_enabled is True
    assert result["enabled"] is True
    assert result["provider"] == "duckduckgo"


def test_disabling_provider_disables_search(monkeypatch) -> None:
    settings = Settings(web_search_provider="duckduckgo")
    monkeypatch.setattr(search, "get_settings", lambda: settings)

    result = search.update_search_config(search.SearchConfigUpdateRequest(provider="disabled"))

    assert settings.web_search_provider == "disabled"
    assert settings.web_search_enabled is False
    assert result["enabled"] is False
