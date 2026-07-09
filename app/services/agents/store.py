"""SQLite persistence for task-linked Agent Runner v1."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.core.config import get_settings

ACTIVE_RUN_STATUSES = {"queued", "planning", "running", "waiting_approval"}


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
    return datetime.now(timezone.utc).isoformat()


def initialize_agent_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_agent_runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, project_id TEXT,
                title TEXT NOT NULL, objective TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', mode TEXT NOT NULL DEFAULT 'assist',
                plan_json TEXT, final_output TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, completed_at TEXT, cancelled_at TEXT,
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_agent_steps (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                step_type TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', input_json TEXT,
                output_text TEXT, error TEXT, requires_approval INTEGER NOT NULL DEFAULT 0,
                approval_status TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                started_at TEXT, completed_at TEXT,
                FOREIGN KEY (run_id) REFERENCES workspace_agent_runs(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_agent_artifacts (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, artifact_type TEXT NOT NULL,
                title TEXT NOT NULL, content TEXT, note_id TEXT, task_id TEXT,
                project_id TEXT, metadata_json TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES workspace_agent_runs(id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_agent_runs_task ON workspace_agent_runs(task_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_agent_runs_status ON workspace_agent_runs(status, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_agent_steps_run ON workspace_agent_steps(run_id, step_index)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_agent_artifacts_run ON workspace_agent_artifacts(run_id)"
        )
        _ensure_column(conn, "workspace_agent_runs", "forked_from_run_id", "TEXT")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def insert_run(run: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_runs (
                id, task_id, project_id, title, objective, status, mode, plan_json,
                final_output, error, created_at, updated_at, started_at, completed_at, cancelled_at
            ) VALUES (
                :id, :task_id, :project_id, :title, :objective, :status, :mode, :plan_json,
                :final_output, :error, :created_at, :updated_at, :started_at, :completed_at, :cancelled_at
            )
        """,
            {**run, "plan_json": json.dumps(run.get("plan", []))},
        )
        if run.get("forked_from_run_id"):
            conn.execute(
                "UPDATE workspace_agent_runs SET forked_from_run_id=? WHERE id=?",
                (run["forked_from_run_id"], run["id"]),
            )
        conn.commit()
        return get_run(run["id"]) or run
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM workspace_agent_runs WHERE id = ?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None
    finally:
        conn.close()


def list_runs(
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        where = ["1=1"]
        params: list = []
        for key, value in (("task_id", task_id), ("project_id", project_id), ("status", status)):
            if value:
                where.append(f"{key} = ?")
                params.append(value)
        where_sql = " AND ".join(where)
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM workspace_agent_runs WHERE {where_sql}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT * FROM workspace_agent_runs WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_run(row) for row in rows], total
    finally:
        conn.close()


def update_run(run_id: str, updates: dict) -> dict | None:
    return _update("workspace_agent_runs", "id", run_id, updates, _row_to_run)


def insert_step(step: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_steps (
                id, run_id, step_index, step_type, title, status, input_json,
                output_text, error, requires_approval, approval_status,
                created_at, updated_at, started_at, completed_at
            ) VALUES (
                :id, :run_id, :step_index, :step_type, :title, :status, :input_json,
                :output_text, :error, :requires_approval, :approval_status,
                :created_at, :updated_at, :started_at, :completed_at
            )
        """,
            {
                **step,
                "input_json": json.dumps(step.get("input", {})),
                "requires_approval": int(bool(step.get("requires_approval"))),
            },
        )
        conn.commit()
        return get_step(step["id"]) or step
    finally:
        conn.close()


def get_step(step_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_steps WHERE id = ?", (step_id,)
        ).fetchone()
        return _row_to_step(row) if row else None
    finally:
        conn.close()


def list_steps(run_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_steps WHERE run_id = ? ORDER BY step_index", (run_id,)
        ).fetchall()
        return [_row_to_step(row) for row in rows]
    finally:
        conn.close()


def update_step(step_id: str, updates: dict) -> dict | None:
    return _update("workspace_agent_steps", "id", step_id, updates, _row_to_step)


def insert_artifact(artifact: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_artifacts (
                id, run_id, artifact_type, title, content, note_id, task_id,
                project_id, metadata_json, created_at
            ) VALUES (
                :id, :run_id, :artifact_type, :title, :content, :note_id, :task_id,
                :project_id, :metadata_json, :created_at
            )
        """,
            {**artifact, "metadata_json": json.dumps(artifact.get("metadata", {}))},
        )
        conn.commit()
        return artifact
    finally:
        conn.close()


def list_artifacts(run_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_artifacts WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
        return [_row_to_artifact(row) for row in rows]
    finally:
        conn.close()


def cancel_run(run_id: str) -> dict | None:
    run = get_run(run_id)
    if run is None or run["status"] not in ACTIVE_RUN_STATUSES:
        return run
    now = now_iso()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_agent_runs SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=?",
            (now, now, run_id),
        )
        conn.execute(
            """
            UPDATE workspace_agent_steps SET status='cancelled', updated_at=?, completed_at=?
            WHERE run_id=? AND status IN ('pending','running','waiting_approval')
        """,
            (now, now, run_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id)


def recover_interrupted_runs() -> int:
    now = now_iso()
    conn = _connect()
    try:
        cursor = conn.execute(
            """
            UPDATE workspace_agent_runs
            SET status='interrupted', error='Run interrupted by backend restart.', updated_at=?
            WHERE status IN ('queued','planning','running')
        """,
            (now,),
        )
        conn.execute(
            """
            UPDATE workspace_agent_steps SET status='interrupted', error='Interrupted by backend restart.', updated_at=?, completed_at=?
            WHERE status='running'
        """,
            (now, now),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def _update(table: str, key: str, value: str, updates: dict, converter):
    allowed = {
        "workspace_agent_runs": {
            "status",
            "plan",
            "final_output",
            "error",
            "started_at",
            "completed_at",
            "cancelled_at",
            "forked_from_run_id",
        },
        "workspace_agent_steps": {
            "status",
            "input",
            "output_text",
            "error",
            "approval_status",
            "started_at",
            "completed_at",
        },
    }[table]
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return get_run(value) if table.endswith("runs") else get_step(value)
    if "plan" in clean:
        clean["plan_json"] = json.dumps(clean.pop("plan"))
    if "input" in clean:
        clean["input_json"] = json.dumps(clean.pop("input"))
    clean["updated_at"] = now_iso()
    columns = ", ".join(f"{name} = ?" for name in clean)
    conn = _connect()
    try:
        conn.execute(f"UPDATE {table} SET {columns} WHERE {key} = ?", [*clean.values(), value])
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (value,)).fetchone()
        return converter(row) if row else None
    finally:
        conn.close()


def _row_to_run(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("plan_json", None)
    data["plan"] = json.loads(raw) if raw else []
    return data


def _row_to_step(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("input_json", None)
    data["input"] = json.loads(raw) if raw else {}
    data["requires_approval"] = bool(data["requires_approval"])
    return data


def _row_to_artifact(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw = data.pop("metadata_json", None)
    data["metadata"] = json.loads(raw) if raw else {}
    return data
