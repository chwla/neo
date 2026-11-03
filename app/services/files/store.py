"""SQLite persistence for the controlled file workspace."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings

logger = logging.getLogger(__name__)


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
            CREATE TABLE IF NOT EXISTS workspace_file_operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
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
                access TEXT NOT NULL DEFAULT 'managed',
                status TEXT NOT NULL DEFAULT 'registered', file_count INTEGER NOT NULL DEFAULT 0,
                indexed_file_count INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0, metadata_json TEXT,
                deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, indexed_at TEXT,
                external_unsafe_opt_in INTEGER NOT NULL DEFAULT 0,
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
        # Added after the table shipped. It defaults to 'managed', so every row
        # that predates live workspaces keeps the copy-and-deliver behaviour it
        # was created with rather than silently gaining direct write access.
        _ensure_column(conn, "workspace_repos", "access", "TEXT NOT NULL DEFAULT 'managed'")
        # The second of the two gates on unsafe external-agent execution. It is
        # per-repository on purpose: "I accept an unsandboxed agent in this
        # scratch checkout" must not silently mean "in every folder I attach".
        # Defaults to 0, so upgrading grants nothing.
        _ensure_column(
            conn, "workspace_repos", "external_unsafe_opt_in", "INTEGER NOT NULL DEFAULT 0"
        )
        # Delete moves a Files item to Trash (``deleted``, stamped ``deleted_at``).
        # Deleting it permanently stamps ``purged_at``: it is no Files item any more,
        # and its row and bytes go once nothing else uses them (``purge_files``).
        _ensure_column(conn, "workspace_files", "deleted_at", "TEXT")
        _ensure_column(conn, "workspace_files", "purged_at", "TEXT")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    try:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


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


def get_file_by_sha(sha256: str, *, source_type: str | None = None) -> dict | None:
    """The first live row holding these bytes, optionally only one made by ``source_type``.

    Deduplication passes ``source_type``. Rows of different origins have different
    lifetimes -- Notes collects its own files once nothing refers to them -- so a
    Files upload that reused a Notes row could be deleted by that collection.
    """

    conn = _connect()
    try:
        if source_type is None:
            row = conn.execute(
                "SELECT * FROM workspace_files WHERE sha256 = ? AND deleted = 0 "
                "ORDER BY created_at LIMIT 1",
                (sha256,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM workspace_files WHERE sha256 = ? AND deleted = 0 "
                "AND coalesce(source_type, 'upload') = ? ORDER BY created_at LIMIT 1",
                (sha256, source_type),
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
    trashed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        # Locked notes' attachments are ciphertext that only the note can read;
        # listing them as files would show opaque blobs nobody can open. A file
        # deleted permanently is no Files item, even while another item keeps it.
        where = ["coalesce(f.source_type, '') != 'note_encrypted'", "f.purged_at IS NULL"]
        params: list = []
        if trashed:
            where.append("f.deleted = 1")
        elif not include_deleted:
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


#: Where other items point at a file row. A row named in any of these is a shared
#: asset: deleting its Files item permanently leaves the row and its bytes to them.
_REFERENCES = (
    ("gallery_items", "file_id"),
    ("note_attachment_refs", "file_id"),
    ("workspace_repo_files", "file_id"),
    ("workspace_patch_applications", "file_id"),
    ("workspace_patch_application_files", "workspace_file_id"),
    ("workspace_code_symbols", "file_id"),
    ("workspace_code_file_summaries", "file_id"),
    ("workspace_code_references", "source_file_id"),
)


def _referenced(conn: sqlite3.Connection, file_id: str) -> bool:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for table, column in _REFERENCES:
        if (
            table in tables
            and conn.execute(
                f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (file_id,)
            ).fetchone()
        ):
            return True
    return False


def trash_file(file_id: str) -> dict | None:
    """Move a Files item to Trash. Its row, links and bytes stay, so it can come back."""

    conn = _connect()
    try:
        now = now_iso()
        with conn:
            changed = conn.execute(
                "UPDATE workspace_files SET deleted = 1, deleted_at = coalesce(deleted_at, ?), "
                "updated_at = ? WHERE id = ? AND purged_at IS NULL",
                (now, now, file_id),
            ).rowcount
    finally:
        conn.close()
    return get_file(file_id, include_deleted=True) if changed else None


def restore_file(file_id: str) -> dict | None:
    """Bring a Files item back from Trash; ``None`` when it is not in Trash."""

    conn = _connect()
    try:
        with conn:
            changed = conn.execute(
                "UPDATE workspace_files SET deleted = 0, deleted_at = NULL, updated_at = ? "
                "WHERE id = ? AND deleted = 1 AND purged_at IS NULL",
                (now_iso(), file_id),
            ).rowcount
    finally:
        conn.close()
    return get_file(file_id) if changed else None


def _enqueue_collection(conn: sqlite3.Connection, file_ids: list[str], paths: list[str]) -> None:
    now = now_iso()
    conn.execute(
        "INSERT INTO workspace_file_operations "
        "(id, kind, payload_json, state, attempts, created_at, updated_at) "
        "VALUES (?, 'collect', ?, 'pending', 0, ?, ?)",
        (str(uuid.uuid4()), json.dumps({"file_ids": file_ids, "paths": paths}), now, now),
    )


def purge_files(file_ids: list[str] | None = None) -> list[str]:
    """Delete Files items in Trash permanently: every one of them when ``file_ids`` is None.

    Each stops being a Files item at once: ``purged_at`` is stamped and its links go.
    In the same transaction its row is queued for collection, so a crash leaves
    either nothing done or a queued collection that ``run_pending_operations``
    finishes. Returns the ids purged; one not in Trash is left alone.
    """

    conn = _connect()
    try:
        with conn:
            if file_ids is None:
                rows = conn.execute(
                    "SELECT id FROM workspace_files WHERE deleted = 1 AND purged_at IS NULL"
                ).fetchall()
            else:
                rows = [
                    conn.execute(
                        "SELECT id FROM workspace_files "
                        "WHERE id = ? AND deleted = 1 AND purged_at IS NULL",
                        (file_id,),
                    ).fetchone()
                    for file_id in file_ids
                ]
            ids = [row["id"] for row in rows if row]
            if ids:
                now = now_iso()
                conn.executemany(
                    "UPDATE workspace_files SET purged_at = ?, updated_at = ? WHERE id = ?",
                    [(now, now, file_id) for file_id in ids],
                )
                conn.executemany(
                    "DELETE FROM workspace_file_links WHERE file_id = ?", [(i,) for i in ids]
                )
                _enqueue_collection(conn, ids, [])
        return ids
    finally:
        conn.close()


def queue_collection(file_ids: list[str], paths: list[str] | None = None) -> None:
    """Queue rows to collect if nothing uses them, and derived files already orphaned."""

    conn = _connect()
    try:
        with conn:
            _enqueue_collection(conn, list(file_ids), list(paths or []))
    finally:
        conn.close()


def has_pending_operations() -> bool:
    conn = _connect()
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM workspace_file_operations WHERE state = 'pending' LIMIT 1"
            ).fetchone()
        )
    finally:
        conn.close()


def run_pending_operations(owned_roots: list[Path]) -> int:
    """Finish queued collections. Safe to run any number of times, from any process.

    A row is deleted only once it is no Files item and nothing else uses it, in a
    transaction that also records its path; the bytes are unlinked after that, so a
    row never names a file that is gone. Only paths inside ``owned_roots`` are ever
    unlinked, never a file in an attached repository. A failed unlink (a file held
    open on Windows, say) leaves the operation pending for the next run to finish.
    """

    roots = [Path(root).expanduser().resolve() for root in owned_roots]
    conn = _connect()
    done = 0
    try:
        ops = conn.execute(
            "SELECT * FROM workspace_file_operations WHERE state = 'pending' ORDER BY created_at"
        ).fetchall()
        for op in ops:
            payload = json.loads(op["payload_json"])
            try:
                with conn:
                    paths = list(payload.get("paths") or [])
                    for file_id in payload.get("file_ids") or []:
                        row = conn.execute(
                            "SELECT storage_path, purged_at FROM workspace_files WHERE id = ?",
                            (file_id,),
                        ).fetchone()
                        if row is None or row["purged_at"] is None or _referenced(conn, file_id):
                            continue
                        conn.execute("DELETE FROM workspace_files WHERE id = ?", (file_id,))
                        paths.append(row["storage_path"])
                    payload = {"file_ids": [], "paths": paths}
                    conn.execute(
                        "UPDATE workspace_file_operations SET payload_json = ?, updated_at = ? "
                        "WHERE id = ?",
                        (json.dumps(payload), now_iso(), op["id"]),
                    )
                for raw in payload["paths"]:
                    path = Path(raw).expanduser().resolve()
                    if not any(path.is_relative_to(root) for root in roots):
                        logger.warning("file_collection_outside_owned_roots path=%s", path)
                        continue
                    path.unlink(missing_ok=True)
                with conn:
                    conn.execute(
                        "UPDATE workspace_file_operations SET state = 'done', updated_at = ? "
                        "WHERE id = ?",
                        (now_iso(), op["id"]),
                    )
                done += 1
            except Exception as exc:  # noqa: BLE001 - left pending; the next run retries
                logger.warning("file_collection_failed op=%s error=%s", op["id"], exc)
                with conn:
                    conn.execute(
                        "UPDATE workspace_file_operations SET attempts = attempts + 1, "
                        "last_error = ?, updated_at = ? WHERE id = ?",
                        (str(exc)[:500], now_iso(), op["id"]),
                    )
        return done
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
