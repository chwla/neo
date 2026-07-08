"""SQLite persistence for the controlled file workspace."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings


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


def initialize_workspace_file_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_files (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, original_filename TEXT NOT NULL,
                display_name TEXT NOT NULL, mime_type TEXT, extension TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT, storage_path TEXT NOT NULL,
                extracted_text TEXT, summary TEXT, source_type TEXT NOT NULL DEFAULT 'upload',
                source_id TEXT, metadata_json TEXT, deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_file_links (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL, link_type TEXT NOT NULL,
                target_id TEXT NOT NULL, title TEXT, metadata_json TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES workspace_files(id),
                UNIQUE(file_id, link_type, target_id)
            );
            CREATE TABLE IF NOT EXISTS workspace_artifacts (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, artifact_type TEXT NOT NULL,
                content TEXT NOT NULL, source_type TEXT, source_id TEXT, project_id TEXT,
                task_id TEXT, note_id TEXT, agent_run_id TEXT, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_patch_applications (
                id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, file_id TEXT NOT NULL,
                task_id TEXT, project_id TEXT, agent_run_id TEXT, status TEXT NOT NULL,
                original_sha256 TEXT NOT NULL, new_sha256 TEXT, original_content TEXT NOT NULL,
                new_content TEXT, patch_text TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,
                applied_at TEXT,
                FOREIGN KEY (artifact_id) REFERENCES workspace_artifacts(id),
                FOREIGN KEY (file_id) REFERENCES workspace_files(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_patch_application_files (
                id TEXT PRIMARY KEY,
                patch_application_id TEXT NOT NULL,
                repo_id TEXT,
                workspace_file_id TEXT,
                repo_file_id TEXT,
                relative_path TEXT NOT NULL,
                change_type TEXT NOT NULL,
                status TEXT NOT NULL,
                original_sha256 TEXT,
                new_sha256 TEXT,
                original_size_bytes INTEGER,
                new_size_bytes INTEGER,
                original_content TEXT,
                new_content TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (patch_application_id) REFERENCES workspace_patch_applications(id),
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (workspace_file_id) REFERENCES workspace_files(id),
                FOREIGN KEY (repo_file_id) REFERENCES workspace_repo_files(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_repos (
                id TEXT PRIMARY KEY, project_id TEXT, name TEXT NOT NULL,
                original_path TEXT NOT NULL, workspace_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'registered', file_count INTEGER NOT NULL DEFAULT 0,
                indexed_file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0, metadata_json TEXT,
                deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, indexed_at TEXT,
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_repo_files (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, file_id TEXT NOT NULL,
                relative_path TEXT NOT NULL, original_relative_path TEXT NOT NULL,
                language TEXT, size_bytes INTEGER NOT NULL DEFAULT 0, sha256 TEXT,
                status TEXT NOT NULL DEFAULT 'indexed', metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (file_id) REFERENCES workspace_files(id),
                UNIQUE(repo_id, relative_path)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_indexes (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'ready', file_count INTEGER NOT NULL DEFAULT 0,
                indexed_file_count INTEGER NOT NULL DEFAULT 0,
                symbol_count INTEGER NOT NULL DEFAULT 0,
                dependency_count INTEGER NOT NULL DEFAULT 0,
                route_count INTEGER NOT NULL DEFAULT 0, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, indexed_at TEXT,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_symbols (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, repo_file_id TEXT NOT NULL,
                file_id TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL,
                qualified_name TEXT, symbol_type TEXT NOT NULL, language TEXT,
                line_start INTEGER, line_end INTEGER, signature TEXT,
                parent_symbol_id TEXT, doc_text TEXT, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (repo_file_id) REFERENCES workspace_repo_files(id),
                FOREIGN KEY (file_id) REFERENCES workspace_files(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_dependencies (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
                source_repo_file_id TEXT NOT NULL, target_repo_file_id TEXT,
                source_relative_path TEXT NOT NULL, target_relative_path TEXT,
                import_text TEXT NOT NULL, dependency_type TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0, metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_file_summaries (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, repo_file_id TEXT NOT NULL UNIQUE,
                file_id TEXT NOT NULL, relative_path TEXT NOT NULL, language TEXT,
                summary TEXT NOT NULL, purpose TEXT, key_symbols_json TEXT,
                metadata_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (repo_file_id) REFERENCES workspace_repo_files(id),
                FOREIGN KEY (file_id) REFERENCES workspace_files(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_references (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL, symbol_id TEXT,
                referenced_name TEXT NOT NULL, reference_type TEXT NOT NULL,
                source_repo_file_id TEXT NOT NULL, source_file_id TEXT NOT NULL,
                source_relative_path TEXT NOT NULL, line_start INTEGER, line_end INTEGER,
                column_start INTEGER, column_end INTEGER, context_text TEXT,
                resolved INTEGER NOT NULL DEFAULT 0, confidence REAL NOT NULL DEFAULT 0.5,
                metadata_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (symbol_id) REFERENCES workspace_code_symbols(id),
                FOREIGN KEY (source_repo_file_id) REFERENCES workspace_repo_files(id),
                FOREIGN KEY (source_file_id) REFERENCES workspace_files(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_symbol_relationships (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
                source_symbol_id TEXT NOT NULL, target_symbol_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.5,
                metadata_json TEXT, created_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (source_symbol_id) REFERENCES workspace_code_symbols(id),
                FOREIGN KEY (target_symbol_id) REFERENCES workspace_code_symbols(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_code_related_files (
                id TEXT PRIMARY KEY, repo_id TEXT NOT NULL,
                source_repo_file_id TEXT NOT NULL, target_repo_file_id TEXT NOT NULL,
                relationship_type TEXT NOT NULL, score REAL NOT NULL DEFAULT 0.0,
                metadata_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (repo_id) REFERENCES workspace_repos(id),
                FOREIGN KEY (source_repo_file_id) REFERENCES workspace_repo_files(id),
                FOREIGN KEY (target_repo_file_id) REFERENCES workspace_repo_files(id),
                UNIQUE(repo_id, source_repo_file_id, target_repo_file_id, relationship_type)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_files_visible
            ON workspace_files(deleted, updated_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_files_sha ON workspace_files(sha256);
            CREATE INDEX IF NOT EXISTS idx_workspace_file_links_target
            ON workspace_file_links(link_type, target_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_file_links_file
            ON workspace_file_links(file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_task
            ON workspace_artifacts(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_project
            ON workspace_artifacts(project_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_artifacts_agent_run
            ON workspace_artifacts(agent_run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_applications_artifact
            ON workspace_patch_applications(artifact_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_applications_file
            ON workspace_patch_applications(file_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_applications_task
            ON workspace_patch_applications(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_application_files_application
            ON workspace_patch_application_files(patch_application_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_application_files_repo
            ON workspace_patch_application_files(repo_id, relative_path);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_application_files_workspace_file
            ON workspace_patch_application_files(workspace_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_patch_application_files_repo_file
            ON workspace_patch_application_files(repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_repos_project
            ON workspace_repos(project_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_repos_visible
            ON workspace_repos(deleted, updated_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_repo_files_repo_path
            ON workspace_repo_files(repo_id, relative_path);
            CREATE INDEX IF NOT EXISTS idx_workspace_repo_files_file
            ON workspace_repo_files(file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_repo_files_sha
            ON workspace_repo_files(repo_id, sha256);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_indexes_repo
            ON workspace_code_indexes(repo_id, updated_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbols_repo_name
            ON workspace_code_symbols(repo_id, name);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbols_repo_type
            ON workspace_code_symbols(repo_id, symbol_type);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbols_file
            ON workspace_code_symbols(repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbols_path
            ON workspace_code_symbols(repo_id, relative_path);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_dependencies_repo_source
            ON workspace_code_dependencies(repo_id, source_repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_dependencies_repo_target
            ON workspace_code_dependencies(repo_id, target_repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_file_summaries_repo_path
            ON workspace_code_file_summaries(repo_id, relative_path);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_references_repo_name
            ON workspace_code_references(repo_id, referenced_name);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_references_symbol
            ON workspace_code_references(symbol_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_references_file
            ON workspace_code_references(source_repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_references_path
            ON workspace_code_references(repo_id, source_relative_path);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbol_relationships_source
            ON workspace_code_symbol_relationships(source_symbol_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_symbol_relationships_target
            ON workspace_code_symbol_relationships(target_symbol_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_related_files_source
            ON workspace_code_related_files(repo_id, source_repo_file_id);
            CREATE INDEX IF NOT EXISTS idx_workspace_code_related_files_target
            ON workspace_code_related_files(repo_id, target_repo_file_id);
        """)
        conn.commit()
    finally:
        conn.close()


def insert_file(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_files (
                id, filename, original_filename, display_name, mime_type, extension, size_bytes,
                sha256, storage_path, extracted_text, summary, source_type, source_id,
                metadata_json, deleted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                item["id"],
                item["filename"],
                item["original_filename"],
                item["display_name"],
                item.get("mime_type"),
                item.get("extension"),
                item["size_bytes"],
                item.get("sha256"),
                item["storage_path"],
                item.get("extracted_text"),
                item.get("summary"),
                item.get("source_type", "upload"),
                item.get("source_id"),
                json.dumps(item.get("metadata", {})),
                int(item.get("deleted", False)),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_file(item["id"], include_deleted=True) or item
    finally:
        conn.close()


def get_file(file_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM workspace_files WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted = 0"
        row = conn.execute(sql, (file_id,)).fetchone()
        return _file_row(row) if row else None
    finally:
        conn.close()


def get_file_by_sha(sha256: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_files WHERE sha256 = ? AND deleted = 0 "
            "ORDER BY created_at LIMIT 1",
            (sha256,),
        ).fetchone()
        return _file_row(row) if row else None
    finally:
        conn.close()


def list_files(
    *,
    q: str | None = None,
    extension: str | None = None,
    link_type: str | None = None,
    target_id: str | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        where = ["1 = 1"]
        params: list = []
        if not include_deleted:
            where.append("f.deleted = 0")
        if extension:
            where.append("lower(coalesce(f.extension, '')) = ?")
            params.append(extension.lower().lstrip("."))
        if q:
            like = f"%{q.lower()}%"
            where.append(
                "(lower(f.filename) LIKE ? OR lower(f.display_name) LIKE ? "
                "OR lower(coalesce(f.extension, '')) LIKE ? "
                "OR lower(coalesce(f.extracted_text, '')) LIKE ? "
                "OR lower(coalesce(f.summary, '')) LIKE ?)"
            )
            params.extend([like] * 5)
        if link_type and target_id:
            where.append(
                "EXISTS (SELECT 1 FROM workspace_file_links l WHERE l.file_id = f.id "
                "AND l.link_type = ? AND l.target_id = ?)"
            )
            params.extend([link_type, target_id])
        where_sql = " AND ".join(where)
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_files f WHERE {where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT f.* FROM workspace_files f WHERE {where_sql} "
            "ORDER BY f.updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_file_row(row) for row in rows], int(total)
    finally:
        conn.close()


def update_file(file_id: str, updates: dict) -> dict | None:
    conn = _connect()
    try:
        columns, params = [], []
        for key in (
            "summary",
            "deleted",
            "sha256",
            "size_bytes",
            "extracted_text",
            "metadata_json",
        ):
            if key in updates:
                columns.append(f"{key} = ?")
                if key == "deleted":
                    params.append(int(updates[key]))
                elif key == "metadata_json":
                    params.append(json.dumps(updates[key]))
                else:
                    params.append(updates[key])
        if not columns:
            return get_file(file_id, include_deleted=True)
        columns.append("updated_at = ?")
        params.extend([updates.get("updated_at") or now_iso(), file_id])
        conn.execute(f"UPDATE workspace_files SET {', '.join(columns)} WHERE id = ?", params)
        conn.commit()
        return get_file(file_id, include_deleted=True)
    finally:
        conn.close()


def insert_link(link: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO workspace_file_links
            (id, file_id, link_type, target_id, title, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
            (
                link["id"],
                link["file_id"],
                link["link_type"],
                link["target_id"],
                link.get("title"),
                json.dumps(link.get("metadata", {})),
                link["created_at"],
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM workspace_file_links WHERE file_id = ? "
            "AND link_type = ? AND target_id = ?",
            (link["file_id"], link["link_type"], link["target_id"]),
        ).fetchone()
        return _link_row(row)
    finally:
        conn.close()


def list_links(file_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_file_links WHERE file_id = ? ORDER BY created_at", (file_id,)
        ).fetchall()
        return [_link_row(row) for row in rows]
    finally:
        conn.close()


def delete_link(file_id: str, link_id: str) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM workspace_file_links WHERE id = ? AND file_id = ?", (link_id, file_id)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def insert_artifact(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_artifacts (id, title, artifact_type, content, source_type,
            source_id, project_id, task_id, note_id, agent_run_id, metadata_json,
            created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                item["id"],
                item["title"],
                item["artifact_type"],
                item["content"],
                item.get("source_type"),
                item.get("source_id"),
                item.get("project_id"),
                item.get("task_id"),
                item.get("note_id"),
                item.get("agent_run_id"),
                json.dumps(item.get("metadata", {})),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
        return get_artifact(item["id"]) or item
    finally:
        conn.close()


def get_artifact(artifact_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _artifact_row(row) if row else None
    finally:
        conn.close()


def list_artifacts(
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    artifact_type: str | None = None,
) -> list[dict]:
    conn = _connect()
    try:
        where, params = ["1 = 1"], []
        for column, value in (
            ("project_id", project_id),
            ("task_id", task_id),
            ("agent_run_id", agent_run_id),
            ("artifact_type", artifact_type),
        ):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        rows = conn.execute(
            f"SELECT * FROM workspace_artifacts WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [_artifact_row(row) for row in rows]
    finally:
        conn.close()


def _json(value: str | None) -> dict:
    try:
        return json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _file_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", None))
    item["deleted"] = bool(item["deleted"])
    return item


def _link_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", None))
    return item


def _artifact_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["metadata"] = _json(item.pop("metadata_json", None))
    return item
