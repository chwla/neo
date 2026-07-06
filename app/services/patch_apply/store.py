from __future__ import annotations

import sqlite3

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


def insert_application(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_patch_applications (
                id, artifact_id, file_id, task_id, project_id, agent_run_id, status,
                original_sha256, new_sha256, original_content, new_content, patch_text,
                error, created_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["artifact_id"],
                item["file_id"],
                item.get("task_id"),
                item.get("project_id"),
                item.get("agent_run_id"),
                item["status"],
                item["original_sha256"],
                item.get("new_sha256"),
                item["original_content"],
                item.get("new_content"),
                item["patch_text"],
                item.get("error"),
                item["created_at"],
                item.get("applied_at"),
            ),
        )
        conn.commit()
        return get_application(item["id"]) or item
    finally:
        conn.close()


def update_application(application_id: str, updates: dict) -> dict | None:
    allowed = {"status", "new_sha256", "new_content", "error", "applied_at"}
    columns, params = [], []
    for key, value in updates.items():
        if key in allowed:
            columns.append(f"{key} = ?")
            params.append(value)
    if not columns:
        return get_application(application_id)
    conn = _connect()
    try:
        params.append(application_id)
        conn.execute(
            f"UPDATE workspace_patch_applications SET {', '.join(columns)} WHERE id = ?",
            params,
        )
        conn.commit()
        return get_application(application_id)
    finally:
        conn.close()


def get_application(application_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_patch_applications WHERE id = ?", (application_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_applications(
    *,
    artifact_id: str | None = None,
    file_id: str | None = None,
    task_id: str | None = None,
    project_id: str | None = None,
    agent_run_id: str | None = None,
) -> list[dict]:
    where, params = ["1 = 1"], []
    for column, value in (
        ("artifact_id", artifact_id),
        ("file_id", file_id),
        ("task_id", task_id),
        ("project_id", project_id),
        ("agent_run_id", agent_run_id),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_patch_applications WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
