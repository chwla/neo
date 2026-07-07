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


def initialize_test_runner_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_test_commands (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                project_id TEXT,
                name TEXT NOT NULL,
                command_json TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                timeout_seconds INTEGER NOT NULL DEFAULT 120,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_test_runs (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                agent_run_id TEXT,
                patch_application_id TEXT,
                test_command_id TEXT,
                name TEXT NOT NULL,
                command_json TEXT NOT NULL,
                working_directory TEXT NOT NULL,
                status TEXT NOT NULL,
                exit_code INTEGER,
                stdout_text TEXT,
                stderr_text TEXT,
                combined_output TEXT,
                duration_ms INTEGER,
                timeout_seconds INTEGER NOT NULL,
                error TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id),
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id),
                FOREIGN KEY (agent_run_id) REFERENCES workspace_agent_runs(id),
                FOREIGN KEY (patch_application_id) REFERENCES workspace_patch_applications(id),
                FOREIGN KEY (test_command_id) REFERENCES workspace_test_commands(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_test_commands_repo
            ON workspace_test_commands(repo_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_test_runs_repo
            ON workspace_test_runs(repo_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_test_runs_task
            ON workspace_test_runs(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_test_runs_agent
            ON workspace_test_runs(agent_run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_test_runs_patch
            ON workspace_test_runs(patch_application_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_test_runs_status
            ON workspace_test_runs(status, created_at);
        """)
        conn.commit()
    finally:
        conn.close()


def insert_command(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_test_commands (
                id, repo_id, project_id, name, command_json, working_directory,
                timeout_seconds, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                item["id"],
                item["repo_id"],
                item.get("project_id"),
                item["name"],
                json.dumps(item["command"]),
                item["working_directory"],
                item["timeout_seconds"],
                int(item.get("enabled", True)),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_command(item["id"]) or item
    finally:
        conn.close()


def get_command(command_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_test_commands WHERE id = ?", (command_id,)
        ).fetchone()
        return _command_row(row) if row else None
    finally:
        conn.close()


def list_commands(repo_id: str, *, include_disabled: bool = True) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM workspace_test_commands WHERE repo_id = ?"
        if not include_disabled:
            sql += " AND enabled = 1"
        try:
            rows = conn.execute(sql + " ORDER BY created_at", (repo_id,)).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return []
        return [_command_row(row) for row in rows]
    finally:
        conn.close()


def update_command(command_id: str, updates: dict) -> dict | None:
    allowed = {"name", "command", "working_directory", "timeout_seconds", "enabled", "updated_at"}
    columns, params = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        column = "command_json" if key == "command" else key
        columns.append(f"{column} = ?")
        params.append(
            json.dumps(value) if key == "command" else int(value) if key == "enabled" else value
        )
    if not columns:
        return get_command(command_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_test_commands SET {', '.join(columns)} WHERE id = ?",
            [*params, command_id],
        )
        conn.commit()
        return get_command(command_id)
    finally:
        conn.close()


def insert_run(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_test_runs (
                id, repo_id, project_id, task_id, agent_run_id, patch_application_id,
                test_command_id, name, command_json, working_directory, status, exit_code,
                stdout_text, stderr_text, combined_output, duration_ms, timeout_seconds,
                error, metadata_json, created_at, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                item["id"],
                item["repo_id"],
                item.get("project_id"),
                item.get("task_id"),
                item.get("agent_run_id"),
                item.get("patch_application_id"),
                item.get("test_command_id"),
                item["name"],
                json.dumps(item["command"]),
                item["working_directory"],
                item["status"],
                item.get("exit_code"),
                item.get("stdout_text", ""),
                item.get("stderr_text", ""),
                item.get("combined_output", ""),
                item.get("duration_ms"),
                item["timeout_seconds"],
                item.get("error"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item.get("started_at"),
                item.get("completed_at"),
            ),
        )
        conn.commit()
        return get_run(item["id"]) or item
    finally:
        conn.close()


def get_run(run_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM workspace_test_runs WHERE id = ?", (run_id,)).fetchone()
        return _run_row(row) if row else None
    finally:
        conn.close()


def update_run(run_id: str, updates: dict) -> dict | None:
    allowed = {
        "status",
        "exit_code",
        "stdout_text",
        "stderr_text",
        "combined_output",
        "duration_ms",
        "error",
        "metadata",
        "started_at",
        "completed_at",
    }
    columns, params = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        column = "metadata_json" if key == "metadata" else key
        columns.append(f"{column} = ?")
        params.append(json.dumps(value) if key == "metadata" else value)
    conn = _connect()
    try:
        if columns:
            conn.execute(
                f"UPDATE workspace_test_runs SET {', '.join(columns)} WHERE id = ?",
                [*params, run_id],
            )
            conn.commit()
        return get_run(run_id)
    finally:
        conn.close()


def list_runs(
    *,
    repo_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    patch_application_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1 = 1"], []
    for column, value in (
        ("repo_id", repo_id),
        ("project_id", project_id),
        ("task_id", task_id),
        ("agent_run_id", agent_run_id),
        ("patch_application_id", patch_application_id),
        ("status", status),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        try:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM workspace_test_runs WHERE {clause}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM workspace_test_runs WHERE {clause} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return [], 0
        return [_run_row(row) for row in rows], total
    finally:
        conn.close()


def _command_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["command"] = json.loads(item.pop("command_json"))
    item["enabled"] = bool(item["enabled"])
    return item


def _run_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["command"] = json.loads(item.pop("command_json"))
    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
    for key in ("stdout_text", "stderr_text", "combined_output"):
        item[key] = item[key] or ""
    return item
