from __future__ import annotations

import sqlite3

import pytest
import requests

from app.api.routes import chat as chat_routes
from app.services.llm import LLMChatResult, ProviderUsagePersistenceError
from app.services.llm_registry import router as registry_router


def test_usage_record_sqlite_failure_is_not_misclassified_as_provider_failure(monkeypatch) -> None:
    client = object.__new__(registry_router.RoutedLLMClient)
    client.route_name = "chat"
    client.last_metadata = {}
    monkeypatch.setattr(
        registry_router,
        "record_call",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )

    with pytest.raises(ProviderUsagePersistenceError, match="database is locked"):
        client._success(
            LLMChatResult(content="answer"),
            {"id": "provider"},
            {"id": "model"},
            False,
        )


def test_chat_failure_diagnostics_distinguish_provider_persistence_and_internal_errors(
    monkeypatch,
) -> None:
    config = type(
        "Config",
        (),
        {
            "name": "Ollama",
            "model": "model",
            "base_url": "http://provider",
            "timeout_seconds": 30,
        },
    )()
    registry = type("Registry", (), {"get": lambda *_: config})
    monkeypatch.setattr(chat_routes, "LLMRegistry", lambda: registry())

    provider = chat_routes._chat_failure(requests.ConnectionError("connection refused"))
    persistence = chat_routes._chat_failure(ProviderUsagePersistenceError("database is locked"))
    internal = chat_routes._chat_failure(RuntimeError("unexpected state"))

    assert provider == (
        502,
        "Provider failed",
        "Ollama did not finish the response. Expected model at http://provider within 30 seconds. "
        "Details: connection refused",
    )
    assert persistence == (
        500,
        "Neo persistence failed",
        "Neo could not persist provider usage data. Details: database is locked",
    )
    assert internal == (500, "Chat failed", "Neo chat failed internally. Details: unexpected state")
