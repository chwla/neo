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


def clear_repo(repo_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM workspace_code_symbol_relationships WHERE repo_id = ?", (repo_id,)
        )
        conn.execute("DELETE FROM workspace_code_references WHERE repo_id = ?", (repo_id,))
        conn.execute("DELETE FROM workspace_code_related_files WHERE repo_id = ?", (repo_id,))
        conn.commit()
    finally:
        conn.close()


def insert_reference(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_references (
                id, repo_id, symbol_id, referenced_name, reference_type,
                source_repo_file_id, source_file_id, source_relative_path,
                line_start, line_end, column_start, column_end, context_text,
                resolved, confidence, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item.get("symbol_id"),
                item["referenced_name"],
                item["reference_type"],
                item["source_repo_file_id"],
                item["source_file_id"],
                item["source_relative_path"],
                item.get("line_start"),
                item.get("line_end"),
                item.get("column_start"),
                item.get("column_end"),
                item.get("context_text"),
                int(item.get("resolved", False)),
                item.get("confidence", 0.5),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def insert_relationship(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_symbol_relationships (
                id, repo_id, source_symbol_id, target_symbol_id, relationship_type,
                confidence, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item["source_symbol_id"],
                item["target_symbol_id"],
                item["relationship_type"],
                item.get("confidence", 0.5),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def insert_related_file(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_code_related_files (
                id, repo_id, source_repo_file_id, target_repo_file_id,
                relationship_type, score, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item["id"],
                item["repo_id"],
                item["source_repo_file_id"],
                item["target_repo_file_id"],
                item["relationship_type"],
                item["score"],
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def list_references(
    *,
    repo_id: str | None = None,
    symbol_id: str | None = None,
    name: str | None = None,
    reference_type: str | None = None,
    source_repo_file_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1 = 1"], []
    for column, value in (
        ("r.repo_id", repo_id),
        ("r.symbol_id", symbol_id),
        ("r.reference_type", reference_type),
        ("r.source_repo_file_id", source_repo_file_id),
    ):
        if value:
            where.append(f"{column} = ?")
            params.append(value)
    if name:
        where.append("lower(r.referenced_name) = ?")
        params.append(name.lower())
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_code_references r WHERE {clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT r.*, s.repo_file_id AS target_repo_file_id "
            "FROM workspace_code_references r "
            "LEFT JOIN workspace_code_symbols s ON s.id = r.symbol_id "
            f"WHERE {clause} ORDER BY r.source_relative_path, r.line_start LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row(row) for row in rows], int(total)
    finally:
        conn.close()


def list_relationships(
    repo_id: str, *, source_symbol_id: str | None = None, target_symbol_id: str | None = None
) -> list[dict]:
    where, params = ["repo_id = ?"], [repo_id]
    if source_symbol_id:
        where.append("source_symbol_id = ?")
        params.append(source_symbol_id)
    if target_symbol_id:
        where.append("target_symbol_id = ?")
        params.append(target_symbol_id)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_code_symbol_relationships WHERE {' AND '.join(where)}",
            params,
        ).fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()


def list_related_files(repo_id: str, source_repo_file_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT rf.*, target.relative_path AS target_relative_path,
                      target.file_id AS target_file_id
               FROM workspace_code_related_files rf
               JOIN workspace_repo_files target ON target.id = rf.target_repo_file_id
               WHERE rf.repo_id = ? AND rf.source_repo_file_id = ?
               ORDER BY rf.score DESC, target.relative_path""",
            (repo_id, source_repo_file_id),
        ).fetchall()
        return [_row(row) for row in rows]
    finally:
        conn.close()


def stats(repo_id: str) -> dict:
    conn = _connect()
    try:
        reference_count = conn.execute(
            "SELECT COUNT(*) FROM workspace_code_references WHERE repo_id = ?", (repo_id,)
        ).fetchone()[0]
        resolved = conn.execute(
            "SELECT COUNT(*) FROM workspace_code_references WHERE repo_id = ? AND resolved = 1",
            (repo_id,),
        ).fetchone()[0]
        relationships = conn.execute(
            "SELECT COUNT(*) FROM workspace_code_symbol_relationships WHERE repo_id = ?",
            (repo_id,),
        ).fetchone()[0]
        related = conn.execute(
            "SELECT COUNT(*) FROM workspace_code_related_files WHERE repo_id = ?", (repo_id,)
        ).fetchone()[0]
        return {
            "reference_count": int(reference_count),
            "resolved_reference_count": int(resolved),
            "relationship_count": int(relationships),
            "related_file_count": int(related),
        }
    finally:
        conn.close()


def set_status(repo_id: str, status: str, metadata: dict) -> None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM workspace_code_indexes WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        if not row:
            raise LookupError("Build Codebase Index before building Symbol Awareness.")
        current = _json(row[0], {})
        current["symbol_awareness"] = {"status": status, **metadata}
        conn.execute(
            "UPDATE workspace_code_indexes SET metadata_json = ?, updated_at = ? WHERE repo_id = ?",
            (json.dumps(current), metadata.get("updated_at"), repo_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_status(repo_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT metadata_json FROM workspace_code_indexes WHERE repo_id = ?", (repo_id,)
        ).fetchone()
        if not row:
            return None
        return _json(row[0], {}).get("symbol_awareness")
    finally:
        conn.close()


def _json(value: str | None, fallback):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _row(row: sqlite3.Row) -> dict:
    item = dict(row)
    if "metadata_json" in item:
        item["metadata"] = _json(item.pop("metadata_json"), {})
    if "resolved" in item:
        item["resolved"] = bool(item["resolved"])
    return item
