from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.services import profile_accounts


def test_application_startup_profile_isolation_and_api_validation(monkeypatch, tmp_path) -> None:
    """Exercise the public profile API without touching a real Neo profile store."""

    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")

    with TestClient(create_app()) as client:
        assert client.get("/api/health/live").status_code == 200
        assert client.post("/api/account-profiles", json={"username": "Alice"}).status_code == 422

        alice = client.post(
            "/api/account-profiles",
            json={"username": "Alice", "password": "alice-pass"},
        )
        assert alice.status_code == 201
        alice_id = alice.json()["profile"]["id"]
        alice_chat = client.post("/api/chats", json={})
        assert alice_chat.status_code == 201
        alice_chat_id = alice_chat.json()["id"]

        assert client.post("/api/account-profiles/session/end").status_code == 204
        bob = client.post(
            "/api/account-profiles",
            json={"username": "Bob", "password": "bob-pass"},
        )
        assert bob.status_code == 201
        assert client.get(f"/api/chats/{alice_chat_id}").status_code == 404

        assert client.post("/api/account-profiles/session/end").status_code == 204
        wrong_password = client.post(
            f"/api/account-profiles/{alice_id}/unlock",
            json={"password": "wrong"},
        )
        assert wrong_password.status_code == 401
        unlocked = client.post(
            f"/api/account-profiles/{alice_id}/unlock",
            json={"password": "alice-pass"},
        )
        assert unlocked.status_code == 200
        assert client.get(f"/api/chats/{alice_chat_id}").status_code == 200


def test_guest_profile_database_is_ephemeral(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")

    with TestClient(create_app()) as client:
        guest = client.post("/api/account-profiles/guest")
        assert guest.status_code == 200
        assert guest.json()["profile"]["is_guest"] is True
        assert client.post("/api/chats", json={}).status_code == 201
        assert client.post("/api/account-profiles/session/end").status_code == 204
        assert client.get("/api/account-profiles/session/current").status_code == 401
