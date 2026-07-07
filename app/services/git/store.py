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


def initialize_git_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_git_repos (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                project_id TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                git_initialized INTEGER NOT NULL DEFAULT 0,
                current_head TEXT,
                default_branch TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                initialized_at TEXT,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_git_checkpoints (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                agent_run_id TEXT,
                patch_application_id TEXT,
                test_run_id TEXT,
                commit_sha TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT,
                changed_files_json TEXT,
                stats_json TEXT,
                status TEXT NOT NULL DEFAULT 'created',
                created_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id),
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id),
                FOREIGN KEY (agent_run_id) REFERENCES workspace_agent_runs(id),
                FOREIGN KEY (patch_application_id) REFERENCES workspace_patch_applications(id),
                FOREIGN KEY (test_run_id) REFERENCES workspace_test_runs(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_git_operations (
                id TEXT PRIMARY KEY,
                repo_id TEXT NOT NULL,
                checkpoint_id TEXT,
                operation_type TEXT NOT NULL,
                status TEXT NOT NULL,
                stdout_text TEXT,
                stderr_text TEXT,
                error TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (checkpoint_id) REFERENCES workspace_git_checkpoints(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_git_repos_repo
            ON workspace_git_repos(repo_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_git_checkpoints_repo
            ON workspace_git_checkpoints(repo_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_git_checkpoints_task
            ON workspace_git_checkpoints(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_git_checkpoints_patch
            ON workspace_git_checkpoints(patch_application_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_git_operations_repo
            ON workspace_git_operations(repo_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_git_operations_checkpoint
            ON workspace_git_operations(checkpoint_id, created_at);
        """)
        conn.commit()
    finally:
        conn.close()


def insert_git_repo(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_git_repos (
                id, repo_id, project_id, status, git_initialized, current_head,
                default_branch, metadata_json, created_at, updated_at, initialized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item.get("project_id"),
                item["status"],
                int(item.get("git_initialized", False)),
                item.get("current_head"),
                item.get("default_branch"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
                item.get("initialized_at"),
            ),
        )
        conn.commit()
        return get_git_repo(item["repo_id"]) or item
    finally:
        conn.close()


def get_git_repo(repo_id: str) -> dict | None:
    conn = _connect()
    try:
        try:
            row = conn.execute(
                "SELECT * FROM workspace_git_repos WHERE repo_id = ?", (repo_id,)
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        return _git_repo_row(row) if row else None
    finally:
        conn.close()


def update_git_repo(repo_id: str, updates: dict) -> dict | None:
    allowed = {
        "status",
        "git_initialized",
        "current_head",
        "default_branch",
        "metadata",
        "updated_at",
        "initialized_at",
    }
    columns, params = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        column = "metadata_json" if key == "metadata" else key
        columns.append(f"{column} = ?")
        if key == "metadata":
            params.append(json.dumps(value))
        elif key == "git_initialized":
            params.append(int(value))
        else:
            params.append(value)
    conn = _connect()
    try:
        if columns:
            conn.execute(
                f"UPDATE workspace_git_repos SET {', '.join(columns)} WHERE repo_id = ?",
                [*params, repo_id],
            )
            conn.commit()
        return get_git_repo(repo_id)
    finally:
        conn.close()


def insert_checkpoint(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_git_checkpoints (
                id, repo_id, project_id, task_id, agent_run_id, patch_application_id,
                test_run_id, commit_sha, title, message, changed_files_json, stats_json,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item.get("project_id"),
                item.get("task_id"),
                item.get("agent_run_id"),
                item.get("patch_application_id"),
                item.get("test_run_id"),
                item["commit_sha"],
                item["title"],
                item.get("message"),
                json.dumps(item.get("changed_files", [])),
                json.dumps(item.get("stats", {})),
                item.get("status", "created"),
                item["created_at"],
            ),
        )
        conn.commit()
        return get_checkpoint(item["id"]) or item
    finally:
        conn.close()


def get_checkpoint(checkpoint_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_git_checkpoints WHERE id = ?", (checkpoint_id,)
        ).fetchone()
        return _checkpoint_row(row) if row else None
    finally:
        conn.close()


def update_checkpoint(checkpoint_id: str, updates: dict) -> dict | None:
    allowed = {"status", "stats"}
    columns, params = [], []
    for key, value in updates.items():
        if key in allowed:
            column = "stats_json" if key == "stats" else key
            columns.append(f"{column} = ?")
            params.append(json.dumps(value) if key == "stats" else value)
    conn = _connect()
    try:
        if columns:
            conn.execute(
                f"UPDATE workspace_git_checkpoints SET {', '.join(columns)} WHERE id = ?",
                [*params, checkpoint_id],
            )
            conn.commit()
        return get_checkpoint(checkpoint_id)
    finally:
        conn.close()


def list_checkpoints(
    *,
    repo_id: str | None = None,
    task_id: str | None = None,
    patch_application_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1 = 1"], []
    for column, value in (
        ("repo_id", repo_id),
        ("task_id", task_id),
        ("patch_application_id", patch_application_id),
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
                    f"SELECT COUNT(*) FROM workspace_git_checkpoints WHERE {clause}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM workspace_git_checkpoints WHERE {clause} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return [], 0
        return [_checkpoint_row(row) for row in rows], total
    finally:
        conn.close()


def insert_operation(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_git_operations (
                id, repo_id, checkpoint_id, operation_type, status, stdout_text,
                stderr_text, error, metadata_json, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item.get("checkpoint_id"),
                item["operation_type"],
                item["status"],
                item.get("stdout_text", ""),
                item.get("stderr_text", ""),
                item.get("error"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item.get("completed_at"),
            ),
        )
        conn.commit()
        return get_operation(item["id"]) or item
    finally:
        conn.close()


def get_operation(operation_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_git_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        return _operation_row(row) if row else None
    finally:
        conn.close()


def list_operations(
    repo_id: str, *, checkpoint_id: str | None = None, limit: int = 100, offset: int = 0
) -> tuple[list[dict], int]:
    where, params = ["repo_id = ?"], [repo_id]
    if checkpoint_id:
        where.append("checkpoint_id = ?")
        params.append(checkpoint_id)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        try:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM workspace_git_operations WHERE {clause}", params
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"SELECT * FROM workspace_git_operations WHERE {clause} "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return [], 0
        return [_operation_row(row) for row in rows], total
    finally:
        conn.close()


def _git_repo_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["git_initialized"] = bool(item["git_initialized"])
    item["metadata"] = _json(item.pop("metadata_json"), {})
    return item


def _checkpoint_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["changed_files"] = _json(item.pop("changed_files_json"), [])
    item["stats"] = _json(item.pop("stats_json"), {})
    return item


def _operation_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json"), {})
    item["stdout_text"] = item["stdout_text"] or ""
    item["stderr_text"] = item["stderr_text"] or ""
    return item


def _json(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback
