from __future__ import annotations

import json

import app.api.routes.health as health_routes


def test_liveness_is_process_only() -> None:
    assert health_routes.liveness() == {"status": "alive"}


def test_readiness_is_ready_only_when_every_dependency_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        health_routes,
        "_readiness_checks",
        lambda: {
            "storage": {"ok": True},
            "model": {"ok": True},
            "search": {"ok": True},
            "vault": {"ok": True},
            "connectors": {"ok": True, "required": []},
        },
    )

    response = health_routes.readiness()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["status"] == "ready"


def test_readiness_reports_the_failed_dependency(monkeypatch) -> None:
    monkeypatch.setattr(
        health_routes,
        "_readiness_checks",
        lambda: {
            "storage": {"ok": True},
            "model": {"ok": False, "error": "selected model is unavailable"},
            "search": {"ok": True},
            "vault": {"ok": True},
            "connectors": {"ok": True, "required": []},
        },
    )

    response = health_routes.readiness()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["model"]["error"] == "selected model is unavailable"
