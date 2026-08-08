from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.routes import chat as chat_routes
from app.main import create_app
from app.services import profile_accounts


def test_parallel_first_memory_writes_initialize_once(monkeypatch, tmp_path) -> None:
    """Fresh profile writes must not race creating the canonical schema ledger."""

    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")
    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Parallel memory", "password": "local-pass"},
        )
        assert created_profile.status_code == 201

        def create_memory(index: int):
            return client.post(
                "/api/memory",
                json={
                    "value": f"parallel fact {index}",
                    "display_text": f"parallel fact {index}",
                    "memory_type": "knowledge",
                    "domain": "learning",
                    "slot": f"knowledge:learning:item:{uuid4()}",
                    "cardinality": "additive",
                    "client_mutation_id": f"parallel-first-write-{index}",
                },
            )

        with ThreadPoolExecutor(max_workers=12) as executor:
            responses = list(executor.map(create_memory, range(24)))

        assert [response.status_code for response in responses] == [201] * 24
        assert len(client.get("/api/memory").json()) == 24


def test_parallel_independent_memory_writes_replan_without_api_conflicts(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")
    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Burst memory", "password": "local-pass"},
        )
        assert created_profile.status_code == 201

        def create_memory(index: int):
            return client.post(
                "/api/memory",
                json={
                    "value": f"independent burst fact {index}",
                    "display_text": f"independent burst fact {index}",
                    "memory_type": "knowledge",
                    "domain": "learning",
                    "slot": f"knowledge:learning:item:{uuid4()}",
                    "cardinality": "additive",
                    "client_mutation_id": f"parallel-burst-{index}",
                },
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            responses = list(executor.map(create_memory, range(48)))

        assert [response.status_code for response in responses] == [201] * 48
        assert len(client.get("/api/memory").json()) == 48


def test_memory_api_namespace_is_canonical_only() -> None:
    with TestClient(create_app()) as client:
        paths = set(client.get("/openapi.json").json()["paths"])

    assert {
        "/api/memory",
        "/api/memory/{memory_id}",
        "/api/memory/candidates",
        "/api/memory/candidates/{candidate_id}/accept",
        "/api/memory/candidates/{candidate_id}/reject",
        "/api/memory/health",
    } <= paths
    assert not any(path.startswith("/api/memory/items") for path in paths)
    assert "/api/workspace-memory/items" in paths


def test_memory_api_supports_list_create_edit_and_forget(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")

    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Memory UI", "password": "local-pass"},
        )
        assert created_profile.status_code == 201
        profile = created_profile.json()["profile"]

        assert client.get("/api/memory").json() == []
        created = client.post(
            "/api/memory",
            json={
                "value": "Concise explanations",
                "display_text": "Concise explanations",
                "memory_type": "preference",
                "domain": "communication",
                "cardinality": "exclusive",
            },
        )
        assert created.status_code == 201, created.text
        memory = created.json()
        assert memory["memory_type"] == "preference"
        assert memory["domain"] == "communication"

        updated = client.patch(
            f"/api/memory/{memory['id']}",
            json={
                "value": "Concise explanations with reasons",
                "display_text": "Concise explanations with reasons",
                "expected_revision": memory["revision"],
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["display_text"] == "Concise explanations with reasons"
        assert len(client.get("/api/memory").json()) == 1

        forgotten = client.delete(f"/api/memory/{memory['id']}")
        assert forgotten.status_code == 200, forgotten.text
        assert client.get("/api/memory").json() == []

    database = tmp_path / "profiles" / "accounts" / profile["id"] / "neo.db"
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT status, display_text FROM memory_records WHERE id = ?", (memory["id"],)
        ).fetchone()
        assert row == ("forgotten", None)
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_relations WHERE owner_id = ?", (profile["owner_id"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_memory_api_reconfirms_equivalent_manual_memory_without_duplicate_source(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")
    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Manual reconfirmation", "password": "local-pass"},
        )
        assert created_profile.status_code == 201
        profile = created_profile.json()["profile"]
        slot = f"knowledge:learning:item:{uuid4()}"
        payload = {
            "value": "Uses examples while learning",
            "display_text": "Uses examples while learning",
            "memory_type": "knowledge",
            "domain": "learning",
            "slot": slot,
            "cardinality": "additive",
        }
        first = client.post(
            "/api/memory", json={**payload, "client_mutation_id": "manual-reconfirm-first"}
        )
        second = client.post(
            "/api/memory", json={**payload, "client_mutation_id": "manual-reconfirm-second"}
        )
        assert first.status_code == second.status_code == 201
        assert second.json()["id"] == first.json()["id"]
        assert second.json()["revision"] == first.json()["revision"] + 1
        assert client.get("/api/memory").json() == [second.json()]

    database = tmp_path / "profiles" / "accounts" / profile["id"] / "neo.db"
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT count(*) FROM memory_sources WHERE memory_id = ?", (first.json()["id"],)
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_memory_api_exclusive_value_edit_supersedes_predecessor(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")
    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Corrected goal", "password": "local-pass"},
        )
        assert created_profile.status_code == 201
        profile = created_profile.json()["profile"]
        original = client.post(
            "/api/memory",
            json={
                "value": "Create long-form cinematic YouTube videos",
                "display_text": "Create long-form cinematic YouTube videos",
                "memory_type": "goal",
                "domain": "video_creation",
                "slot": "goal:video_creation:current_primary_goal",
                "cardinality": "exclusive",
            },
        )
        assert original.status_code == 201, original.text

        corrected = client.patch(
            f"/api/memory/{original.json()['id']}",
            json={
                "value": "Create short Instagram reels",
                "display_text": "Create short Instagram reels",
                "expected_revision": original.json()["revision"],
            },
        )
        assert corrected.status_code == 200, corrected.text
        assert corrected.json()["id"] != original.json()["id"]
        assert corrected.json()["display_text"] == "Create short Instagram reels"
        assert client.get("/api/memory").json() == [corrected.json()]

    database = tmp_path / "profiles" / "accounts" / profile["id"] / "neo.db"
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, status, display_text FROM memory_records ORDER BY created_at, id"
        ).fetchall()
        assert {row[0]: row[1:] for row in rows} == {
            original.json()["id"]: (
                "superseded",
                "Create long-form cinematic YouTube videos",
            ),
            corrected.json()["id"]: ("active", "Create short Instagram reels"),
        }
        assert connection.execute(
            "SELECT count(*) FROM memory_relations "
            "WHERE from_memory_id = ? AND to_memory_id = ? AND relation_type = 'supersedes'",
            (corrected.json()["id"], original.json()["id"]),
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_chat_edit_and_delete_are_zero_memory_call_when_request_is_gated(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(profile_accounts, "_root", lambda: tmp_path / "profiles")

    with TestClient(create_app()) as client:
        created_profile = client.post(
            "/api/account-profiles",
            json={"username": "Memory gates", "password": "local-pass"},
        )
        assert created_profile.status_code == 201
        profile = created_profile.json()["profile"]
        edit_chat = client.post("/api/chats", json={}).json()
        delete_chat = client.post("/api/chats", json={}).json()
        database = tmp_path / "profiles" / "accounts" / profile["id"] / "neo.db"
        connection = sqlite3.connect(database)
        try:
            edit_message = connection.execute(
                "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, 'user', ?)",
                (edit_chat["id"], "original edit text"),
            ).lastrowid
            connection.execute(
                "INSERT INTO chat_messages (chat_id, role, content) VALUES (?, 'user', ?)",
                (delete_chat["id"], "original delete text"),
            )
            connection.commit()
        finally:
            connection.close()

        def forbidden(*_args, **_kwargs):
            raise AssertionError("memory component called")

        monkeypatch.setattr(chat_routes, "build_memory_runtime", forbidden)
        edited = client.patch(
            f"/api/chats/{edit_chat['id']}/messages/{edit_message}",
            json={
                "content": "replacement text",
                "memory_enabled": False,
                "memory_incognito": False,
            },
        )
        assert edited.status_code == 200, edited.text
        deleted = client.delete(f"/api/chats/{delete_chat['id']}?memory_incognito=true")
        assert deleted.status_code == 204, deleted.text

    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT content FROM chat_messages WHERE id = ?", (edit_message,)
        ).fetchone() == ("replacement text",)
        assert connection.execute(
            "SELECT count(*) FROM chats WHERE id = ?", (delete_chat["id"],)
        ).fetchone() == (0,)
    finally:
        connection.close()
