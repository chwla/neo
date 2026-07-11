from __future__ import annotations

# ruff: noqa: E501
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_command_sandbox_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_command_runs (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,command_json TEXT NOT NULL,cwd TEXT NOT NULL,category TEXT NOT NULL,status TEXT NOT NULL,approval_required INTEGER NOT NULL DEFAULT 1,approved INTEGER NOT NULL DEFAULT 0,exit_code INTEGER,stdout_text TEXT,stderr_text TEXT,output_truncated INTEGER NOT NULL DEFAULT 0,duration_ms INTEGER,timeout_ms INTEGER,policy_decision_json TEXT,redaction_summary_json TEXT,created_by TEXT,created_at TEXT NOT NULL,started_at TEXT,completed_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_command_runs_workspace ON workspace_command_runs(workspace_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_command_runs_status ON workspace_command_runs(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_command_runs_category ON workspace_command_runs(category,created_at DESC);
        """)
        conn.commit()
    finally:
        conn.close()


def _row(row):
    if not row:
        return None
    item = dict(row)
    for key in ("command", "policy_decision", "redaction_summary"):
        item[key] = json.loads(item.pop(f"{key}_json") or ("[]" if key == "command" else "{}"))
    item["approval_required"] = bool(item["approval_required"])
    item["approved"] = bool(item["approved"])
    item["output_truncated"] = bool(item["output_truncated"])
    return item


def create(item: dict) -> dict:
    now = now_iso()
    data = {
        **item,
        "id": item.get("id") or str(uuid.uuid4()),
        "created_at": now,
        "approval_required": 1,
        "approved": 0,
        "output_truncated": 0,
        "command_json": json.dumps(item["command"]),
        "policy_decision_json": json.dumps(item["policy_decision"]),
        "redaction_summary_json": "{}",
        "exit_code": None,
        "stdout_text": None,
        "stderr_text": None,
        "duration_ms": None,
        "started_at": None,
        "completed_at": None,
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_command_runs VALUES (:id,:workspace_id,:command_json,:cwd,:category,:status,:approval_required,:approved,:exit_code,:stdout_text,:stderr_text,:output_truncated,:duration_ms,:timeout_ms,:policy_decision_json,:redaction_summary_json,:created_by,:created_at,:started_at,:completed_at)",
            data,
        )
        conn.commit()
        return get(data["id"])
    finally:
        conn.close()


def get(run_id: str) -> dict | None:
    conn = _connect()
    try:
        return _row(
            conn.execute("SELECT * FROM workspace_command_runs WHERE id=?", (run_id,)).fetchone()
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def list_runs(workspace_id: str | None = None, limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        sql = (
            "SELECT * FROM workspace_command_runs"
            + (" WHERE workspace_id=?" if workspace_id else "")
            + " ORDER BY created_at DESC LIMIT ?"
        )
        return [
            _row(row)
            for row in conn.execute(
                sql, (*([workspace_id] if workspace_id else []), limit)
            ).fetchall()
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def update(run_id: str, updates: dict) -> dict | None:
    values = dict(updates)
    for key in ("command", "policy_decision", "redaction_summary"):
        if key in values:
            values[f"{key}_json"] = json.dumps(values.pop(key))
    if not values:
        return get(run_id)
    conn = _connect()
    try:
        columns = list(values)
        conn.execute(
            f"UPDATE workspace_command_runs SET {', '.join(f'{key}=?' for key in columns)} WHERE id=?",
            [values[key] for key in columns] + [run_id],
        )
        conn.commit()
        return get(run_id)
    finally:
        conn.close()
