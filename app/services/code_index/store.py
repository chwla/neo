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


def clear_repo_index(repo_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM workspace_code_symbol_relationships WHERE repo_id = ?", (repo_id,)
        )
        conn.execute("DELETE FROM workspace_code_references WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM workspace_code_related_files WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM workspace_code_symbols WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM workspace_code_dependencies WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM workspace_code_file_summaries WHERE repo_id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()


def upsert_index(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_code_indexes (
                id, repo_id, status, file_count, indexed_file_count, symbol_count,
                dependency_count, route_count, metadata_json, created_at, updated_at, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo_id) DO UPDATE SET
                status=excluded.status, file_count=excluded.file_count,
                indexed_file_count=excluded.indexed_file_count,
                symbol_count=excluded.symbol_count,
                dependency_count=excluded.dependency_count,
                route_count=excluded.route_count, metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at, indexed_at=excluded.indexed_at
            """,
            (
                item["id"],
                item["repo_id"],
                item["status"],
                item["file_count"],
                item["indexed_file_count"],
                item["symbol_count"],
                item["dependency_count"],
                item["route_count"],
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
                item.get("indexed_at"),
            ),
        )
        conn.commit()
        return get_index(item["repo_id"]) or item
    finally:
        conn.close()


def get_index(repo_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_code_indexes WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def mark_stale(repo_id: str, reason: str, updated_at: str) -> None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM workspace_code_indexes WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        if not row:
            return
        metadata = _json(row[0], {})
        metadata["stale_reason"] = reason
        awareness = metadata.get("symbol_awareness")
        if isinstance(awareness, dict):
            metadata["symbol_awareness"] = {
                **awareness,
                "status": "stale",
                "updated_at": updated_at,
                "stale_reason": reason,
            }
        conn.execute(
            "UPDATE workspace_code_indexes SET status = 'stale', metadata_json = ?, "
            "updated_at = ? WHERE repo_id = ?",
            (json.dumps(metadata), updated_at, repo_id),
        )
        conn.commit()
    finally:
        conn.close()


def insert_symbol(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_symbols (
                id, repo_id, repo_file_id, file_id, relative_path, name, qualified_name,
                symbol_type, language, line_start, line_end, signature, parent_symbol_id,
                doc_text, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item["repo_file_id"],
                item["file_id"],
                item["relative_path"],
                item["name"],
                item.get("qualified_name"),
                item["symbol_type"],
                item.get("language"),
                item.get("line_start"),
                item.get("line_end"),
                item.get("signature"),
                item.get("parent_symbol_id"),
                item.get("doc_text"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_symbol(item["id"]) or item
    finally:
        conn.close()


def insert_dependency(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_dependencies (
                id, repo_id, source_repo_file_id, target_repo_file_id,
                source_relative_path, target_relative_path, import_text,
                dependency_type, resolved, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item["source_repo_file_id"],
                item.get("target_repo_file_id"),
                item["source_relative_path"],
                item.get("target_relative_path"),
                item["import_text"],
                item["dependency_type"],
                int(item.get("resolved", False)),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def insert_summary(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_file_summaries (
                id, repo_id, repo_file_id, file_id, relative_path, language, summary,
                purpose, key_symbols_json, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item["repo_file_id"],
                item["file_id"],
                item["relative_path"],
                item.get("language"),
                item["summary"],
                item.get("purpose"),
                json.dumps(item.get("key_symbols", [])),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_summary(item["repo_file_id"]) or item
    finally:
        conn.close()


def list_symbols(
    repo_id: str,
    *,
    q: str | None = None,
    symbol_type: str | None = None,
    language: str | None = None,
    relative_path: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["repo_id = ?"], [repo_id]
    for column, value in (
        ("symbol_type", symbol_type),
        ("language", language),
        ("relative_path", relative_path),
    ):
        if value:
            where.append(f"lower({column}) = ?")
            params.append(value.lower())
    if q:
        where.append(
            "(lower(name) LIKE ? OR lower(coalesce(qualified_name,'')) LIKE ? "
            "OR lower(coalesce(signature,'')) LIKE ?)"
        )
        params.extend([f"%{q.lower()}%"] * 3)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_code_symbols WHERE {clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM workspace_code_symbols WHERE {clause} "
            "ORDER BY relative_path, line_start LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row(row) for row in rows], int(total)
    finally:
        conn.close()


def get_symbol(symbol_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_code_symbols WHERE id = ?", (symbol_id,)
        ).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def list_dependencies(repo_id: str, relative_path: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        sql, params = "SELECT * FROM workspace_code_dependencies WHERE repo_id = ?", [repo_id]
        if relative_path:
            sql += " AND (source_relative_path = ? OR target_relative_path = ?)"
            params.extend([relative_path, relative_path])
        sql += " ORDER BY source_relative_path, import_text"
        return [_row(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_summary(repo_file_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_code_file_summaries WHERE repo_file_id = ?", (repo_file_id,)
        ).fetchone()
        return _row(row, key_symbols=True) if row else None
    finally:
        conn.close()


def list_summaries(repo_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_code_file_summaries WHERE repo_id = ? ORDER BY relative_path",
            (repo_id,),
        ).fetchall()
        return [_row(row, key_symbols=True) for row in rows]
    finally:
        conn.close()


def _json(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row(row: sqlite3.Row, *, key_symbols: bool = False) -> dict:
    item = dict(row)
    if "metadata_json" in item:
        item["metadata"] = _json(item.pop("metadata_json"), {})
    if key_symbols and "key_symbols_json" in item:
        item["key_symbols"] = _json(item.pop("key_symbols_json"), [])
    if "resolved" in item:
        item["resolved"] = bool(item["resolved"])
    return item
