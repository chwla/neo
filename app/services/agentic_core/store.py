from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

JSON_COLUMNS = {
    "state_json": "state",
    "plan_json": "plan",
    "completion_criteria_json": "completion_criteria",
    "context_budget_json": "context_budget",
}


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


def initialize_agentic_core_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_agentic_runs (
                id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                source_run_id TEXT,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                plan_json TEXT,
                completion_criteria_json TEXT,
                context_budget_json TEXT,
                final_report TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_agentic_steps (
                id TEXT PRIMARY KEY,
                agentic_run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                phase TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT,
                output_json TEXT,
                tool_calls_json TEXT,
                verification_json TEXT,
                reflection_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (agentic_run_id) REFERENCES workspace_agentic_runs(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agentic_runs_status_created "
            "ON workspace_agentic_runs(status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agentic_runs_source "
            "ON workspace_agentic_runs(run_type, source_run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agentic_steps_run_index "
            "ON workspace_agentic_steps(agentic_run_id, step_index)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agentic_steps_phase_created "
            "ON workspace_agentic_steps(phase, created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def insert_run(run: dict[str, Any]) -> dict[str, Any]:
    values = _serialize_run(run)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agentic_runs (
                id, run_type, source_run_id, objective, status, state_json, plan_json,
                completion_criteria_json, context_budget_json, final_report,
                created_at, updated_at, completed_at
            ) VALUES (
                :id, :run_type, :source_run_id, :objective, :status, :state_json, :plan_json,
                :completion_criteria_json, :context_budget_json, :final_report,
                :created_at, :updated_at, :completed_at
            )
        """,
            values,
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run["id"]) or run


def get_run(run_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM workspace_agentic_runs WHERE id=?", (run_id,)).fetchone()
        return _run(row) if row else None
    finally:
        conn.close()


def find_by_source(run_type: str, source_run_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        try:
            row = conn.execute(
                "SELECT * FROM workspace_agentic_runs WHERE run_type=? AND source_run_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (run_type, source_run_id),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc).lower():
                return None
            raise
        return _run(row) if row else None
    finally:
        conn.close()


def list_runs(
    *, status: str | None = None, run_type: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[dict[str, Any]], int]:
    where = ["1=1"]
    params: list[Any] = []
    if status:
        where.append("status=?")
        params.append(status)
    if run_type:
        where.append("run_type=?")
        params.append(run_type)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM workspace_agentic_runs WHERE {clause}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT * FROM workspace_agentic_runs WHERE {clause} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_run(row) for row in rows], total
    finally:
        conn.close()


def update_run(run_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {
        "source_run_id",
        "status",
        "state",
        "plan",
        "completion_criteria",
        "context_budget",
        "final_report",
        "completed_at",
    }
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        return get_run(run_id)
    serialized: dict[str, Any] = {}
    for key, value in clean.items():
        column = next((name for name, decoded in JSON_COLUMNS.items() if decoded == key), key)
        serialized[column] = json.dumps(value, sort_keys=True) if column in JSON_COLUMNS else value
    serialized["updated_at"] = now_iso()
    columns = ", ".join(f"{key}=?" for key in serialized)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_agentic_runs SET {columns} WHERE id=?",
            [*serialized.values(), run_id],
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id)


def insert_step(step: dict[str, Any]) -> dict[str, Any]:
    encoded = dict(step)
    for name in ("input", "output", "tool_calls", "verification", "reflection"):
        encoded[f"{name}_json"] = json.dumps(encoded.pop(name, None), sort_keys=True)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agentic_steps (
                id, agentic_run_id, step_index, phase, title, status, input_json,
                output_json, tool_calls_json, verification_json, reflection_json,
                error, created_at, completed_at
            ) VALUES (
                :id, :agentic_run_id, :step_index, :phase, :title, :status, :input_json,
                :output_json, :tool_calls_json, :verification_json, :reflection_json,
                :error, :created_at, :completed_at
            )
        """,
            encoded,
        )
        conn.commit()
    finally:
        conn.close()
    return get_step(step["id"]) or step


def get_step(step_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agentic_steps WHERE id=?", (step_id,)
        ).fetchone()
        return _step(row) if row else None
    finally:
        conn.close()


def list_steps(run_id: str) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agentic_steps WHERE agentic_run_id=? "
            "ORDER BY step_index, created_at",
            (run_id,),
        ).fetchall()
        return [_step(row) for row in rows]
    finally:
        conn.close()


def _serialize_run(run: dict[str, Any]) -> dict[str, Any]:
    values = dict(run)
    for column, decoded in JSON_COLUMNS.items():
        values[column] = json.dumps(values.pop(decoded, None), sort_keys=True)
    return values


def _run(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for column, decoded in JSON_COLUMNS.items():
        raw = data.pop(column, None)
        data[decoded] = (
            json.loads(raw) if raw else ([] if decoded in {"plan", "completion_criteria"} else {})
        )
    return data


def _step(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for name in ("input", "output", "tool_calls", "verification", "reflection"):
        raw = data.pop(f"{name}_json", None)
        data[name] = json.loads(raw) if raw else ([]) if name == "tool_calls" else {}
    return data
