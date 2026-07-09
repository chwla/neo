from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_coding_agent_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_coding_agent_runs (
                id TEXT PRIMARY KEY,
                agent_run_id TEXT NOT NULL,
                task_id TEXT,
                project_id TEXT,
                repo_id TEXT,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                current_iteration INTEGER NOT NULL DEFAULT 1,
                max_iterations INTEGER NOT NULL DEFAULT 3,
                selected_files_json TEXT,
                patch_artifact_id TEXT,
                patch_application_id TEXT,
                test_run_id TEXT,
                checkpoint_id TEXT,
                error TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                cancelled_at TEXT,
                FOREIGN KEY (agent_run_id) REFERENCES workspace_agent_runs(id),
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id),
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_agent_action_requests (
                id TEXT PRIMARY KEY,
                coding_run_id TEXT NOT NULL,
                agent_run_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                title TEXT NOT NULL,
                description TEXT,
                payload_json TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                decided_at TEXT,
                executed_at TEXT,
                FOREIGN KEY (coding_run_id) REFERENCES workspace_coding_agent_runs(id),
                FOREIGN KEY (agent_run_id) REFERENCES workspace_agent_runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_agent
            ON workspace_coding_agent_runs(agent_run_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_task
            ON workspace_coding_agent_runs(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_repo
            ON workspace_coding_agent_runs(repo_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_status
            ON workspace_coding_agent_runs(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_action_requests_run
            ON workspace_agent_action_requests(coding_run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_action_requests_status
            ON workspace_agent_action_requests(status, updated_at);
        """)
        _ensure_column(conn, "workspace_coding_agent_runs", "forked_from_run_id", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "recovery_state", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "last_recoverable_at", "TEXT")
        _ensure_column(conn, "workspace_coding_agent_runs", "agent_definition_id", "TEXT")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_coding_agent_runs_fork
            ON workspace_coding_agent_runs(forked_from_run_id)
        """)
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


RUN_JSON = {"selected_files": "selected_files_json", "metadata": "metadata_json"}
ACTION_JSON = {"payload": "payload_json", "result": "result_json"}


def insert_run(item: dict) -> dict:
    values = {
        **item,
        "selected_files_json": json.dumps(item.get("selected_files", [])),
        "metadata_json": json.dumps(item.get("metadata", {})),
    }
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_coding_agent_runs (
                id, agent_run_id, task_id, project_id, repo_id, objective, status,
                current_iteration, max_iterations, selected_files_json, patch_artifact_id,
                patch_application_id, test_run_id, checkpoint_id, error, metadata_json,
                created_at, updated_at, completed_at, cancelled_at
            ) VALUES (
                :id, :agent_run_id, :task_id, :project_id, :repo_id, :objective, :status,
                :current_iteration, :max_iterations, :selected_files_json, :patch_artifact_id,
                :patch_application_id, :test_run_id, :checkpoint_id, :error, :metadata_json,
                :created_at, :updated_at, :completed_at, :cancelled_at
            )
        """,
            values,
        )
        for column in ("forked_from_run_id", "recovery_state", "last_recoverable_at", "agent_definition_id"):
            if item.get(column) is not None:
                conn.execute(
                    f"UPDATE workspace_coding_agent_runs SET {column}=? WHERE id=?",
                    (item.get(column), item["id"]),
                )
        conn.commit()
        return get_run(item["id"]) or item
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_coding_agent_runs WHERE id=?", (run_id,)
        ).fetchone()
        return _run(row) if row else None
    finally:
        conn.close()


def list_runs(*, task_id=None, project_id=None, repo_id=None, status=None, limit=50, offset=0):
    where, params = ["1=1"], []
    for key, value in (
        ("task_id", task_id),
        ("project_id", project_id),
        ("repo_id", repo_id),
        ("status", status),
    ):
        if value:
            where.append(f"{key}=?")
            params.append(value)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM workspace_coding_agent_runs WHERE {clause}", params
            ).fetchone()[0]
        )
        rows = conn.execute(
            "SELECT * FROM workspace_coding_agent_runs "
            f"WHERE {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_run(row) for row in rows], total
    finally:
        conn.close()


def update_run(run_id: str, updates: dict) -> dict | None:
    return _update("workspace_coding_agent_runs", run_id, updates, RUN_JSON, _run)


def insert_action(item: dict) -> dict:
    values = {
        **item,
        "payload_json": json.dumps(item.get("payload", {})),
        "result_json": json.dumps(item.get("result")) if item.get("result") is not None else None,
    }
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_action_requests (
                id, coding_run_id, agent_run_id, action_type, status, title,
                description, payload_json, result_json, error, created_at, updated_at,
                decided_at, executed_at
            ) VALUES (
                :id, :coding_run_id, :agent_run_id, :action_type, :status, :title,
                :description, :payload_json, :result_json, :error, :created_at, :updated_at,
                :decided_at, :executed_at
            )
        """,
            values,
        )
        conn.commit()
        return get_action(item["id"]) or item
    finally:
        conn.close()


def get_action(action_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_action_requests WHERE id=?", (action_id,)
        ).fetchone()
        return _action(row) if row else None
    finally:
        conn.close()


def list_actions(coding_run_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_action_requests "
            "WHERE coding_run_id=? ORDER BY created_at",
            (coding_run_id,),
        ).fetchall()
        return [_action(row) for row in rows]
    finally:
        conn.close()


def update_action(action_id: str, updates: dict) -> dict | None:
    return _update("workspace_agent_action_requests", action_id, updates, ACTION_JSON, _action)


def cancel_pending_actions(coding_run_id: str) -> None:
    now = now_iso()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_agent_action_requests SET status='cancelled', "
            "updated_at=?, decided_at=? WHERE coding_run_id=? AND status='pending'",
            (now, now, coding_run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _update(table: str, item_id: str, updates: dict, json_fields: dict, converter):
    columns, params = [], []
    for key, value in updates.items():
        column = json_fields.get(key, key)
        columns.append(f"{column}=?")
        params.append(json.dumps(value) if key in json_fields and value is not None else value)
    if not columns:
        return get_run(item_id) if table.endswith("runs") else get_action(item_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE {table} SET {', '.join(columns)} WHERE id=?", [*params, item_id])
        conn.commit()
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        return converter(row) if row else None
    finally:
        conn.close()


def _loads(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _run(row) -> dict:
    item = dict(row)
    item["selected_files"] = _loads(item.pop("selected_files_json", None), [])
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    return item


def _action(row) -> dict:
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    item["result"] = _loads(item.pop("result_json", None), None)
    return item
