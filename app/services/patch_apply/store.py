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
        if not row:
            return None
        item = dict(row)
        item["files"] = list_application_files(application_id, conn=conn)
        return item
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
        ("task_id", task_id),
        ("project_id", project_id),
        ("agent_run_id", agent_run_id),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if file_id:
        where.append(
            "(file_id = ? OR EXISTS (SELECT 1 FROM workspace_patch_application_files paf "
            "WHERE paf.patch_application_id = workspace_patch_applications.id "
            "AND paf.workspace_file_id = ?))"
        )
        params.extend([file_id, file_id])
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_patch_applications WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC",
            params,
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["files"] = list_application_files(item["id"], conn=conn)
            items.append(item)
        return items
    finally:
        conn.close()


def insert_application_file(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_patch_application_files (
                id, patch_application_id, repo_id, workspace_file_id, repo_file_id,
                relative_path, change_type, status, original_sha256, new_sha256,
                original_size_bytes, new_size_bytes, original_content, new_content,
                error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"], item["patch_application_id"], item.get("repo_id"),
                item.get("workspace_file_id"), item.get("repo_file_id"), item["relative_path"],
                item["change_type"], item["status"], item.get("original_sha256"),
                item.get("new_sha256"), item.get("original_size_bytes"),
                item.get("new_size_bytes"), item.get("original_content"),
                item.get("new_content"), item.get("error"), item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_application_file(item["id"]) or item
    finally:
        conn.close()


def update_application_file(file_audit_id: str, updates: dict) -> dict | None:
    allowed = {
        "workspace_file_id", "repo_file_id", "status", "new_sha256", "new_size_bytes",
        "new_content", "error", "updated_at",
    }
    columns, params = [], []
    for key, value in updates.items():
        if key in allowed:
            columns.append(f"{key} = ?")
            params.append(value)
    if not columns:
        return get_application_file(file_audit_id)
    conn = _connect()
    try:
        params.append(file_audit_id)
        conn.execute(
            f"UPDATE workspace_patch_application_files SET {', '.join(columns)} WHERE id = ?",
            params,
        )
        conn.commit()
        return get_application_file(file_audit_id)
    finally:
        conn.close()


def get_application_file(file_audit_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_patch_application_files WHERE id = ?", (file_audit_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_application_files(
    application_id: str, *, conn: sqlite3.Connection | None = None
) -> list[dict]:
    owned = conn is None
    connection = conn or _connect()
    try:
        rows = connection.execute(
            "SELECT * FROM workspace_patch_application_files "
            "WHERE patch_application_id = ? ORDER BY relative_path",
            (application_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            connection.close()
