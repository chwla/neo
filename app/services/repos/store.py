from __future__ import annotations

import json
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


def insert_repo(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_repos (
                id, project_id, name, original_path, workspace_path, status, file_count,
                indexed_file_count, total_bytes, metadata_json, deleted, created_at,
                updated_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item.get("project_id"),
                item["name"],
                item["original_path"],
                item["workspace_path"],
                item["status"],
                item.get("file_count", 0),
                item.get("indexed_file_count", 0),
                item.get("total_bytes", 0),
                json.dumps(item.get("metadata", {})),
                int(item.get("deleted", False)),
                item["created_at"],
                item["updated_at"],
                item.get("indexed_at"),
            ),
        )
        conn.commit()
        return get_repo(item["id"], include_deleted=True) or item
    finally:
        conn.close()


def update_repo(repo_id: str, updates: dict) -> dict | None:
    allowed = {
        "status",
        "file_count",
        "indexed_file_count",
        "total_bytes",
        "metadata_json",
        "deleted",
        "updated_at",
        "indexed_at",
    }
    columns, params = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        columns.append(f"{key} = ?")
        if key == "metadata_json":
            params.append(json.dumps(value))
        elif key == "deleted":
            params.append(int(value))
        else:
            params.append(value)
    if not columns:
        return get_repo(repo_id, include_deleted=True)
    conn = _connect()
    try:
        params.append(repo_id)
        conn.execute(f"UPDATE workspace_repos SET {', '.join(columns)} WHERE id = ?", params)
        conn.commit()
        return get_repo(repo_id, include_deleted=True)
    finally:
        conn.close()


def get_repo(repo_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM workspace_repos WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted = 0"
        row = conn.execute(sql, (repo_id,)).fetchone()
        return _repo_row(row) if row else None
    finally:
        conn.close()


def get_repo_by_original_path(original_path: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_repos WHERE original_path = ? AND deleted = 0",
            (original_path,),
        ).fetchone()
        return _repo_row(row) if row else None
    finally:
        conn.close()


def list_repos(
    *,
    project_id: str | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1 = 1"], []
    if not include_deleted:
        where.append("deleted = 0")
    if project_id:
        where.append("project_id = ?")
        params.append(project_id)
    where_sql = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_repos WHERE {where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM workspace_repos WHERE {where_sql} "
            "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_repo_row(row) for row in rows], int(total)
    finally:
        conn.close()


def insert_repo_file(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_repo_files (
                id, repo_id, file_id, relative_path, original_relative_path, language,
                size_bytes, sha256, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["repo_id"],
                item["file_id"],
                item["relative_path"],
                item["original_relative_path"],
                item.get("language"),
                item["size_bytes"],
                item.get("sha256"),
                item.get("status", "indexed"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_repo_file(item["id"]) or item
    finally:
        conn.close()


def get_repo_file(repo_file_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_repo_files WHERE id = ?", (repo_file_id,)
        ).fetchone()
        return _repo_file_row(row) if row else None
    finally:
        conn.close()


def get_repo_file_by_file_id(file_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_repo_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return _repo_file_row(row) if row else None
    finally:
        conn.close()


def get_repo_file_by_path(repo_id: str, relative_path: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_repo_files WHERE repo_id = ? AND relative_path = ?",
            (repo_id, relative_path),
        ).fetchone()
        return _repo_file_row(row) if row else None
    finally:
        conn.close()


def list_repo_files(
    repo_id: str,
    *,
    q: str | None = None,
    extension: str | None = None,
    language: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["rf.repo_id = ?", "f.deleted = 0"], [repo_id]
    if q:
        like = f"%{q.lower()}%"
        where.append(
            "(lower(rf.relative_path) LIKE ? OR lower(f.filename) LIKE ? "
            "OR lower(coalesce(f.extracted_text, '')) LIKE ? "
            "OR lower(coalesce(f.summary, '')) LIKE ?)"
        )
        params.extend([like] * 4)
    if extension:
        where.append("lower(coalesce(f.extension, '')) = ?")
        params.append(extension.lower().lstrip("."))
    if language:
        where.append("lower(coalesce(rf.language, '')) = ?")
        params.append(language.lower())
    where_sql = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM workspace_repo_files rf "
            "JOIN workspace_files f ON f.id = rf.file_id "
            f"WHERE {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT rf.* FROM workspace_repo_files rf "
            "JOIN workspace_files f ON f.id = rf.file_id "
            f"WHERE {where_sql} ORDER BY rf.relative_path LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_repo_file_row(row) for row in rows], int(total)
    finally:
        conn.close()


def update_repo_file_hash(file_id: str, sha256: str, size_bytes: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_repo_files SET sha256 = ?, size_bytes = ?, updated_at = "
            "(SELECT updated_at FROM workspace_files WHERE id = ?) WHERE file_id = ?",
            (sha256, size_bytes, file_id, file_id),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_failed_import(repo_id: str) -> None:
    """Remove only rows created for a repo whose initial import did not complete."""
    conn = _connect()
    try:
        file_ids = [
            row[0]
            for row in conn.execute(
                "SELECT file_id FROM workspace_repo_files WHERE repo_id = ?", (repo_id,)
            ).fetchall()
        ]
        for file_id in file_ids:
            conn.execute("DELETE FROM workspace_file_links WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM workspace_repo_files WHERE repo_id = ?", (repo_id,))
        for file_id in file_ids:
            conn.execute("DELETE FROM workspace_files WHERE id = ?", (file_id,))
        conn.execute("DELETE FROM workspace_repos WHERE id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()


def _json(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _repo_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", None))
    item["deleted"] = bool(item["deleted"])
    return item


def _repo_file_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", None))
    return item
