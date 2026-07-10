from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings

# ruff: noqa: E501


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_context_memory_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_context_summaries (
          id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
          source_type TEXT NOT NULL, source_id TEXT, summary_text TEXT NOT NULL,
          decisions_json TEXT, constraints_json TEXT, open_items_json TEXT,
          completed_items_json TEXT, files_json TEXT, tests_json TEXT,
          checkpoints_json TEXT, safety_notes_json TEXT, token_estimate_before INTEGER,
          token_estimate_after INTEGER, redaction_summary_json TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_context_events (
          id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
          event_type TEXT NOT NULL, event_ref_id TEXT, importance INTEGER NOT NULL DEFAULT 3,
          content_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_context_summary_scope ON workspace_context_summaries(scope_type, scope_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_summary_source ON workspace_context_summaries(source_type, source_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_event_scope ON workspace_context_events(scope_type, scope_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_event_importance ON workspace_context_events(scope_type, scope_id, importance DESC, created_at DESC);
        """)
        conn.commit()
    finally:
        conn.close()


JSON_COLUMNS = (
    "decisions",
    "constraints",
    "open_items",
    "completed_items",
    "files",
    "tests",
    "checkpoints",
    "safety_notes",
    "redaction_summary",
)


def _summary(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in JSON_COLUMNS:
        item[key] = json.loads(
            item.pop(f"{key}_json") or "[]"
            if key != "redaction_summary"
            else item.pop(f"{key}_json") or "{}"
        )
    return item


def save_summary(item: dict) -> dict:
    now = now_iso()
    values = {
        **item,
        "id": item.get("id") or str(uuid.uuid4()),
        "created_at": item.get("created_at") or now,
        "updated_at": now,
    }
    for key in JSON_COLUMNS:
        values[f"{key}_json"] = json.dumps(
            values.pop(key, {} if key == "redaction_summary" else [])
        )
    conn = _connect()
    try:
        conn.execute(
            """
        INSERT INTO workspace_context_summaries VALUES (:id,:scope_type,:scope_id,:source_type,:source_id,:summary_text,:decisions_json,:constraints_json,:open_items_json,:completed_items_json,:files_json,:tests_json,:checkpoints_json,:safety_notes_json,:token_estimate_before,:token_estimate_after,:redaction_summary_json,:created_at,:updated_at)
        """,
            values,
        )
        conn.commit()
        return get_summary(values["id"])
    finally:
        conn.close()


def get_summary(summary_id: str) -> dict | None:
    conn = _connect()
    try:
        try:
            row = conn.execute(
                "SELECT * FROM workspace_context_summaries WHERE id=?", (summary_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return _summary(row) if row else None
    finally:
        conn.close()


def list_summaries(
    scope_type: str | None = None, scope_id: str | None = None, limit: int = 100
) -> list[dict]:
    where, params = ["1=1"], []
    for key, value in (("scope_type", scope_type), ("scope_id", scope_id)):
        if value:
            where.append(f"{key}=?")
            params.append(value)
    conn = _connect()
    try:
        try:
            rows = conn.execute(
                f"SELECT * FROM workspace_context_summaries WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        except sqlite3.Error:
            return []
        return [_summary(row) for row in rows]
    finally:
        conn.close()


def add_event(
    scope_type: str,
    scope_id: str,
    event_type: str,
    content: dict,
    event_ref_id: str | None = None,
    importance: int = 3,
) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "scope_type": scope_type,
        "scope_id": scope_id,
        "event_type": event_type,
        "event_ref_id": event_ref_id,
        "importance": importance,
        "content_json": json.dumps(content),
        "created_at": now_iso(),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_context_events VALUES (:id,:scope_type,:scope_id,:event_type,:event_ref_id,:importance,:content_json,:created_at)",
            item,
        )
        conn.commit()
        item["content"] = json.loads(item.pop("content_json"))
        return item
    finally:
        conn.close()


def list_events(scope_type: str, scope_id: str, limit: int = 200) -> list[dict]:
    conn = _connect()
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM workspace_context_events WHERE scope_type=? AND scope_id=? ORDER BY importance DESC, created_at ASC LIMIT ?",
                (scope_type, scope_id, limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [{**dict(row), "content": json.loads(row["content_json"])} for row in rows]
    finally:
        conn.close()
