from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_profiles_keep_chat_data_separate_and_require_password():
    client = TestClient(create_app())

    alice = client.post(
        "/api/account-profiles", json={"username": "Alice", "password": "alice-pass"}
    )
    assert alice.status_code == 201
    alice_id = alice.json()["profile"]["id"]
    chat = client.post("/api/chats", json={}).json()

    assert client.post("/api/account-profiles/session/end").status_code == 204
    bob = client.post("/api/account-profiles", json={"username": "Bob", "password": "bob-pass"})
    assert bob.status_code == 201
    assert client.get(f"/api/chats/{chat['id']}").status_code == 404

    assert client.post("/api/account-profiles/session/end").status_code == 204
    assert (
        client.post(
            f"/api/account-profiles/{alice_id}/unlock", json={"password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            f"/api/account-profiles/{alice_id}/unlock", json={"password": "alice-pass"}
        ).status_code
        == 200
    )
    assert client.get(f"/api/chats/{chat['id']}").status_code == 200


def test_guest_profile_is_removed_when_its_session_ends():
    client = TestClient(create_app())
    guest = client.post("/api/account-profiles/guest")
    assert guest.status_code == 200
    assert guest.json()["profile"]["is_guest"] is True
    assert client.post("/api/chats", json={}).status_code == 201
    assert client.post("/api/account-profiles/session/end").status_code == 204
    assert client.get("/api/account-profiles/session/current").status_code == 401
