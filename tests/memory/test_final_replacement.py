from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes import chat as chat_routes
from app.db.memory_migrations import MEMORY_LEDGER_TABLE, MEMORY_TABLES, memory_migration_state
from app.db.session import build_engine
from scripts.inspect_memory import _conversation
from scripts.reset_memory import reset_memory_database
from tests.memory.factories import uuid_string


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE chats (id INTEGER PRIMARY KEY, title TEXT NOT NULL);
            INSERT INTO chats (id, title) VALUES (1, 'preserve me');
            CREATE TABLE memories (id INTEGER PRIMARY KEY, memory_text TEXT);
            INSERT INTO memories (id, memory_text) VALUES (1, 'erase me');
            CREATE TABLE preferences (id INTEGER PRIMARY KEY, value TEXT);
            INSERT INTO preferences (id, value) VALUES (1, 'erase me too');
            CREATE TABLE memory_records_v2 (id TEXT PRIMARY KEY);
            INSERT INTO memory_records_v2 (id) VALUES ('old-canonical-id');
            CREATE TABLE memory_schema_migrations_v2 (revision TEXT PRIMARY KEY);
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_reset_replaces_old_schemas_and_preserves_unrelated_rows(tmp_path) -> None:
    path = tmp_path / "profile.db"
    _database(path)
    owner = uuid_string(901)
    result = reset_memory_database(
        path,
        owner_id=owner,
        database_identity="account-profile:disposable",
    )

    assert result["legacy_memory_tables_remaining"] == 0
    assert result["memory_v2_tables_remaining"] == 0
    assert result["unrelated_tables_modified"] == 0
    assert {"memories", "preferences", "memory_records_v2"} <= set(result["dropped_tables"])

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT title FROM chats").fetchall() == [("preserve me",)]
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert set(MEMORY_TABLES) <= names
        assert MEMORY_LEDGER_TABLE in names
        assert "memories" not in names
        assert "memory_records_v2" not in names
    finally:
        connection.close()

    engine = build_engine(f"sqlite:///{path}")
    try:
        state = memory_migration_state(engine)
        assert state.owner_id == owner
        assert state.database_identity == "account-profile:disposable"
    finally:
        engine.dispose()


def test_reset_is_idempotent(tmp_path) -> None:
    path = tmp_path / "profile.db"
    _database(path)
    owner = uuid_string(902)
    first = reset_memory_database(
        path,
        owner_id=owner,
        database_identity="account-profile:repeatable",
    )
    second = reset_memory_database(
        path,
        owner_id=owner,
        database_identity="account-profile:repeatable",
    )
    assert first["unrelated_tables_modified"] == 0
    assert second["unrelated_tables_modified"] == 0
    assert second["legacy_memory_tables_remaining"] == 0
    assert second["memory_v2_tables_remaining"] == 0


def test_reset_refuses_unexpected_personal_memory_table(tmp_path) -> None:
    path = tmp_path / "profile.db"
    _database(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE memory_unreviewed_cache (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="unexpected_memory_tables:memory_unreviewed_cache"):
        reset_memory_database(
            path,
            owner_id=uuid_string(903),
            database_identity="account-profile:refusal",
        )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT memory_text FROM memories").fetchall() == [("erase me",)]
    finally:
        connection.close()


def test_reset_cli_requires_exact_confirmation() -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/reset_memory.py", "--confirm", "erase_all_memory"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "exact_confirmation_required" in completed.stderr


def test_active_source_has_single_canonical_implementation() -> None:
    root = Path(__file__).resolve().parents[2]
    active_roots = [root / "app", root / "scripts", root / "frontend" / "src"]
    prohibited = (
        "memory_v2",
        "MemoryRecordV2",
        "MemorySourceV2",
        "MemoryRelationV2",
        "MemoryOperationV2",
        "legacy_memory",
        "legacy_read_compatibility",
        "legacy_compatibility",
        "MemoryStore",
        "_sync_memory_embedding",
        "_mark_embedding_stale",
    )
    matches = []
    for base in active_roots:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
                continue
            text = path.read_text(encoding="utf-8")
            matches.extend(
                f"{path.relative_to(root)}:{token}" for token in prohibited if token in text
            )
    assert matches == []

    assert (root / "app" / "repositories" / "memory.py").is_file()
    assert (root / "app" / "services" / "memory" / "coordinator.py").is_file()
    assert (root / "app" / "services" / "memory" / "recall.py").is_file()
    assert (root / "app" / "services" / "memory" / "prompt.py").is_file()
    assert (root / "app" / "services" / "memory" / "extraction.py").is_file()
    assert (root / "app" / "services" / "memory" / "outbox.py").is_file()
    assert len(list((root / "app" / "repositories").glob("memory*.py"))) == 1
    research_ui = (root / "frontend" / "src" / "Research.jsx").read_text(encoding="utf-8")
    assert "api.researchStart" in research_ui
    assert "memory_enabled: memoryEnabled" in research_ui
    assert "incognito: memoryIncognito" in research_ui
    assert "api.researchModeRun" not in research_ui


@pytest.mark.parametrize(
    ("global_incognito", "request_enabled", "request_incognito"),
    ((False, False, False), (False, True, True), (True, True, False)),
)
def test_chat_disabled_and_incognito_do_not_construct_memory_components(
    monkeypatch,
    global_incognito: bool,
    request_enabled: bool,
    request_incognito: bool,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("memory component constructed")

    monkeypatch.setattr(chat_routes, "build_memory_runtime", forbidden)
    monkeypatch.setattr(chat_routes, "build_chat_memory_runtime", forbidden)
    monkeypatch.setattr(
        chat_routes,
        "get_settings",
        lambda: SimpleNamespace(memory_enabled=True, memory_incognito=global_incognito),
    )
    profile = {
        "id": "profile",
        "owner_id": uuid_string(904),
        "is_guest": False,
    }
    with Session(create_engine("sqlite:///:memory:")) as database:
        service = chat_routes._chat_service(
            database,
            profile,
            request_id="chat:1:test",
            ollama=object(),
            rule_result={},
            memory_enabled=request_enabled,
            memory_incognito=request_incognito,
        )
    assert service.memory_enabled is False
    assert service.memory_runtime is None


def test_conversation_inspector_uses_persisted_recall_diagnostics() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(
            """
            CREATE TABLE chat_messages (
                id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                metadata_json TEXT
            );
            CREATE TABLE memory_sources (
                id TEXT, owner_id TEXT, memory_id TEXT, message_id TEXT,
                operation_id TEXT, is_active INTEGER, conversation_id TEXT, created_at TEXT
            );
            CREATE TABLE memory_operations (
                id TEXT, owner_id TEXT, operation_kind TEXT, status TEXT,
                outcome TEXT, result_record_ids TEXT, created_at TEXT
            );
            CREATE TABLE memory_usage_events (
                id TEXT, owner_id TEXT, memory_id TEXT, request_id TEXT,
                session_id TEXT, purpose TEXT, used_at TEXT
            );
            CREATE TABLE memory_candidates (
                id TEXT, owner_id TEXT, state TEXT, trusted_target_ids TEXT,
                source_spans_json TEXT, created_at TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO chat_messages VALUES (?, ?, ?, ?)",
            (
                11,
                7,
                "user",
                '{"memory_diagnostic": {'
                '"recalled_ids": ["recalled-1", "suppressed-1"], '
                '"current_turn_suppressed_ids": ["suppressed-1"], '
                '"final_serialized_ids": ["recalled-1"]}}',
            ),
        )
        result = _conversation(connection, uuid_string(905), "7")
    finally:
        connection.close()

    assert result["user_message_ids"] == ["11"]
    assert result["recalled_ids"] == ["recalled-1", "suppressed-1"]
    assert result["current_turn_suppressed_ids"] == ["suppressed-1"]
    assert result["final_serialized_ids"] == ["recalled-1"]
