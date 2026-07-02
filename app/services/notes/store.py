"""SQLite-backed persistence for user notes."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.core.config import get_settings


def _db_path() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_notes_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                summary TEXT,
                source_type TEXT,
                source_id TEXT,
                source_url TEXT,
                source_title TEXT,
                source_metadata_json TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS note_tags (
                note_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (note_id, tag),
                FOREIGN KEY (note_id) REFERENCES notes(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS note_links (
                id TEXT PRIMARY KEY,
                note_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                target_id TEXT,
                target_url TEXT,
                title TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (note_id) REFERENCES notes(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_source
            ON notes(source_type, source_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_notes_visible_sort
            ON notes(deleted, archived, pinned, updated_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_note_tags_tag
            ON note_tags(tag)
        """)
        conn.commit()
    finally:
        conn.close()


def insert_note(note: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO notes (
                id, title, body, summary, source_type, source_id, source_url,
                source_title, source_metadata_json, pinned, archived, deleted,
                created_at, updated_at
            ) VALUES (
                :id, :title, :body, :summary, :source_type, :source_id, :source_url,
                :source_title, :source_metadata_json, :pinned, :archived, :deleted,
                :created_at, :updated_at
            )
            """,
            _note_params(note),
        )
        _replace_tags(conn, note["id"], note.get("tags", []))
        conn.commit()
        return get_note(note["id"], include_deleted=True) or note
    finally:
        conn.close()


def get_note(note_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM notes WHERE id = ?"
        params: list = [note_id]
        if not include_deleted:
            sql += " AND deleted = 0"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _row_to_note(conn, row)
    finally:
        conn.close()


def find_note_by_source(source_type: str, source_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT * FROM notes
            WHERE source_type = ? AND source_id = ? AND deleted = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (source_type, source_id),
        ).fetchone()
        if row is None:
            return None
        return _row_to_note(conn, row)
    finally:
        conn.close()


def list_notes(
    *,
    q: str | None = None,
    tag: str | None = None,
    include_archived: bool = False,
    pinned_first: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        where = ["n.deleted = 0"]
        params: list = []
        joins = ""
        if not include_archived:
            where.append("n.archived = 0")
        if tag:
            joins += " JOIN note_tags ft ON ft.note_id = n.id"
            where.append("ft.tag = ?")
            params.append(tag)
        if q:
            like = f"%{q.lower()}%"
            where.append(
                """
                (
                    lower(n.title) LIKE ?
                    OR lower(n.body) LIKE ?
                    OR lower(coalesce(n.summary, '')) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM note_tags st
                        WHERE st.note_id = n.id AND lower(st.tag) LIKE ?
                    )
                )
                """
            )
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)
        order_sql = "n.pinned DESC, n.updated_at DESC" if pinned_first else "n.updated_at DESC"
        total = conn.execute(
            f"SELECT COUNT(DISTINCT n.id) FROM notes n{joins} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT DISTINCT n.* FROM notes n{joins}
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_note(conn, row) for row in rows], int(total)
    finally:
        conn.close()


def update_note(note_id: str, updates: dict) -> dict | None:
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM notes WHERE id = ? AND deleted = 0",
            (note_id,),
        ).fetchone()
        if existing is None:
            return None

        columns: list[str] = []
        params: list = []
        for key in (
            "title",
            "body",
            "summary",
            "source_type",
            "source_id",
            "source_url",
            "source_title",
        ):
            if key in updates:
                columns.append(f"{key} = ?")
                params.append(updates[key])
        if "source_metadata" in updates:
            columns.append("source_metadata_json = ?")
            params.append(json.dumps(updates["source_metadata"] or {}))
        if "pinned" in updates:
            columns.append("pinned = ?")
            params.append(1 if updates["pinned"] else 0)
        if "archived" in updates:
            columns.append("archived = ?")
            params.append(1 if updates["archived"] else 0)
        if "deleted" in updates:
            columns.append("deleted = ?")
            params.append(1 if updates["deleted"] else 0)

        columns.append("updated_at = ?")
        params.append(updates.get("updated_at") or now_iso())
        params.append(note_id)
        conn.execute(f"UPDATE notes SET {', '.join(columns)} WHERE id = ?", params)
        if "tags" in updates:
            _replace_tags(conn, note_id, updates["tags"])
        conn.commit()
        return _row_to_note(
            conn,
            conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone(),
        )
    finally:
        conn.close()


def insert_link(link: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO note_links (
                id, note_id, link_type, target_id, target_url, title,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                link["id"],
                link["note_id"],
                link["link_type"],
                link.get("target_id"),
                link.get("target_url"),
                link.get("title"),
                json.dumps(link.get("metadata", {})),
                link["created_at"],
            ),
        )
        conn.commit()
        return link
    finally:
        conn.close()


def list_tags() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT nt.tag, COUNT(*) AS count
            FROM note_tags nt
            JOIN notes n ON n.id = nt.note_id
            WHERE n.deleted = 0
            GROUP BY nt.tag
            ORDER BY count DESC, nt.tag ASC
            """
        ).fetchall()
        return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


def _note_params(note: dict) -> dict:
    return {
        **note,
        "source_metadata_json": json.dumps(note.get("source_metadata", {})),
        "pinned": 1 if note.get("pinned") else 0,
        "archived": 1 if note.get("archived") else 0,
        "deleted": 1 if note.get("deleted") else 0,
    }


def _replace_tags(conn: sqlite3.Connection, note_id: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO note_tags(note_id, tag) VALUES (?, ?)",
        [(note_id, tag) for tag in tags],
    )


def _row_to_note(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_metadata = data.pop("source_metadata_json", None)
    data["source_metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    data["pinned"] = bool(data["pinned"])
    data["archived"] = bool(data["archived"])
    data["deleted"] = bool(data["deleted"])
    tag_rows = conn.execute(
        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag ASC",
        (data["id"],),
    ).fetchall()
    data["tags"] = [row["tag"] for row in tag_rows]
    return data
