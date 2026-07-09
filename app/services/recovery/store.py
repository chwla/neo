from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings


def _db_path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_recovery_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_agent_recovery_events (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status_before TEXT,
                status_after TEXT,
                action_request_id TEXT,
                source_step_id TEXT,
                forked_from_run_id TEXT,
                metadata_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_recovery_events_run
            ON workspace_agent_recovery_events(run_type, run_id, created_at);
        """)
        _ensure_column(conn, "workspace_agent_runs", "forked_from_run_id", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "forked_from_run_id", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "recovery_state", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "last_recoverable_at", "TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_fork
            ON workspace_coding_agent_runs(forked_from_run_id)
        """)
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    try:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def insert_event(
    *,
    run_type: str,
    run_id: str,
    event_type: str,
    status_before: str | None = None,
    status_after: str | None = None,
    action_request_id: str | None = None,
    source_step_id: str | None = None,
    forked_from_run_id: str | None = None,
    metadata: dict | None = None,
    error: str | None = None,
) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "run_type": run_type,
        "run_id": run_id,
        "event_type": event_type,
        "status_before": status_before,
        "status_after": status_after,
        "action_request_id": action_request_id,
        "source_step_id": source_step_id,
        "forked_from_run_id": forked_from_run_id,
        "metadata": metadata or {},
        "error": error,
        "created_at": now_iso(),
    }
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_recovery_events (
                id, run_type, run_id, event_type, status_before, status_after,
                action_request_id, source_step_id, forked_from_run_id,
                metadata_json, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["run_type"],
                item["run_id"],
                item["event_type"],
                item.get("status_before"),
                item.get("status_after"),
                item.get("action_request_id"),
                item.get("source_step_id"),
                item.get("forked_from_run_id"),
                json.dumps(item["metadata"]),
                item.get("error"),
                item["created_at"],
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def list_events(
    *,
    run_type: str | None = None,
    run_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1=1"], []
    if run_type:
        where.append("run_type=?")
        params.append(run_type)
    if run_id:
        where.append("run_id=?")
        params.append(run_id)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM workspace_agent_recovery_events WHERE {clause}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT * FROM workspace_agent_recovery_events "
            f"WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_event(row) for row in rows], total
    finally:
        conn.close()


def _loads(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _event(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item
