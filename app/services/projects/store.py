"""SQLite-backed persistence for organizational Projects v1."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.notes.store import get_note


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


def initialize_project_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_projects (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                priority TEXT NOT NULL DEFAULT 'medium',
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_project_tags (
                project_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (project_id, tag),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_project_links (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                target_id TEXT,
                target_url TEXT,
                title TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_project_notes (
                project_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (project_id, note_id),
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id),
                FOREIGN KEY (note_id) REFERENCES notes(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_projects_visible
            ON workspace_projects(deleted, archived, pinned, updated_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_project_notes_project
            ON workspace_project_notes(project_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_project_notes_note
            ON workspace_project_notes(note_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_workspace_project_tags_tag
            ON workspace_project_tags(tag)
        """)
        conn.commit()
    finally:
        conn.close()


def insert_project(project: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_projects (
                id, title, description, status, priority, pinned, archived,
                deleted, created_at, updated_at
            ) VALUES (
                :id, :title, :description, :status, :priority, :pinned, :archived,
                :deleted, :created_at, :updated_at
            )
            """,
            _project_params(project),
        )
        _replace_tags(conn, project["id"], project.get("tags", []))
        conn.commit()
        return get_project(project["id"], include_deleted=True) or project
    finally:
        conn.close()


def get_project(project_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM workspace_projects WHERE id = ?"
        params: list = [project_id]
        if not include_deleted:
            sql += " AND deleted = 0"
        row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return _row_to_project(conn, row)
    finally:
        conn.close()


def list_projects(
    *,
    q: str | None = None,
    tag: str | None = None,
    status: str | None = None,
    include_archived: bool = False,
    pinned_first: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        joins = ""
        where = ["p.deleted = 0"]
        params: list = []
        if not include_archived:
            where.append("p.archived = 0")
        if status:
            where.append("p.status = ?")
            params.append(status)
        if tag:
            joins += " JOIN workspace_project_tags ft ON ft.project_id = p.id"
            where.append("ft.tag = ?")
            params.append(tag)
        if q:
            like = f"%{q.lower()}%"
            where.append(
                """
                (
                    lower(p.title) LIKE ?
                    OR lower(p.description) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM workspace_project_tags st
                        WHERE st.project_id = p.id AND lower(st.tag) LIKE ?
                    )
                )
                """
            )
            params.extend([like, like, like])
        where_sql = " AND ".join(where)
        pin_sort = "p.pinned DESC, " if pinned_first else ""
        order_sql = (
            f"{pin_sort}"
            "CASE p.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'medium' THEN 2 ELSE 3 END, p.updated_at DESC"
        )
        total = conn.execute(
            f"SELECT COUNT(DISTINCT p.id) FROM workspace_projects p{joins} WHERE {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT DISTINCT p.*,
                (
                    SELECT COUNT(*) FROM workspace_project_notes pn
                    JOIN notes n ON n.id = pn.note_id
                    WHERE pn.project_id = p.id AND n.deleted = 0
                ) AS linked_notes_count
            FROM workspace_projects p{joins}
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_project(conn, row) for row in rows], int(total)
    finally:
        conn.close()


def update_project(project_id: str, updates: dict) -> dict | None:
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM workspace_projects WHERE id = ? AND deleted = 0",
            (project_id,),
        ).fetchone()
        if existing is None:
            return None
        columns: list[str] = []
        params: list = []
        for key in ("title", "description", "status", "priority"):
            if key in updates:
                columns.append(f"{key} = ?")
                params.append(updates[key])
        if "pinned" in updates:
            columns.append("pinned = ?")
            params.append(1 if updates["pinned"] else 0)
        if "archived" in updates:
            columns.append("archived = ?")
            params.append(1 if updates["archived"] else 0)
            if updates["archived"]:
                columns.append("status = ?")
                params.append("archived")
        if "deleted" in updates:
            columns.append("deleted = ?")
            params.append(1 if updates["deleted"] else 0)
        columns.append("updated_at = ?")
        params.append(updates.get("updated_at") or now_iso())
        params.append(project_id)
        conn.execute(f"UPDATE workspace_projects SET {', '.join(columns)} WHERE id = ?", params)
        if "tags" in updates:
            _replace_tags(conn, project_id, updates["tags"])
        conn.commit()
        row = conn.execute(
            "SELECT * FROM workspace_projects WHERE id = ?", (project_id,)
        ).fetchone()
        return _row_to_project(conn, row)
    finally:
        conn.close()


def list_tags() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT pt.tag, COUNT(*) AS count
            FROM workspace_project_tags pt
            JOIN workspace_projects p ON p.id = pt.project_id
            WHERE p.deleted = 0
            GROUP BY pt.tag
            ORDER BY count DESC, pt.tag ASC
            """
        ).fetchall()
        return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


def attach_note(project_id: str, note_id: str) -> bool:
    conn = _connect()
    try:
        project = conn.execute(
            "SELECT id FROM workspace_projects WHERE id = ? AND deleted = 0",
            (project_id,),
        ).fetchone()
        if project is None or get_note(note_id) is None:
            return False
        now = now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO workspace_project_notes(project_id, note_id, created_at)
            VALUES (?, ?, ?)
            """,
            (project_id, note_id, now),
        )
        conn.execute(
            "UPDATE workspace_projects SET updated_at = ? WHERE id = ?",
            (now, project_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def detach_note(project_id: str, note_id: str) -> bool:
    conn = _connect()
    try:
        project = conn.execute(
            "SELECT id FROM workspace_projects WHERE id = ? AND deleted = 0",
            (project_id,),
        ).fetchone()
        if project is None:
            return False
        cursor = conn.execute(
            "DELETE FROM workspace_project_notes WHERE project_id = ? AND note_id = ?",
            (project_id, note_id),
        )
        conn.execute(
            "UPDATE workspace_projects SET updated_at = ? WHERE id = ?",
            (now_iso(), project_id),
        )
        conn.commit()
        return cursor.rowcount >= 0
    finally:
        conn.close()


def list_project_notes(project_id: str) -> list[dict] | None:
    conn = _connect()
    try:
        project = conn.execute(
            "SELECT id FROM workspace_projects WHERE id = ? AND deleted = 0",
            (project_id,),
        ).fetchone()
        if project is None:
            return None
        rows = conn.execute(
            """
            SELECT n.*, pn.created_at AS attached_at
            FROM workspace_project_notes pn
            JOIN notes n ON n.id = pn.note_id
            WHERE pn.project_id = ? AND n.deleted = 0
            ORDER BY n.updated_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [_row_to_note_with_tags(conn, row) for row in rows]
    finally:
        conn.close()


def list_note_projects(note_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT p.*
            FROM workspace_project_notes pn
            JOIN workspace_projects p ON p.id = pn.project_id
            WHERE pn.note_id = ? AND p.deleted = 0
            ORDER BY p.pinned DESC, p.updated_at DESC
            """,
            (note_id,),
        ).fetchall()
        return [_row_to_project(conn, row) for row in rows]
    finally:
        conn.close()


def list_links(project_id: str) -> list[dict] | None:
    conn = _connect()
    try:
        project = conn.execute(
            "SELECT id FROM workspace_projects WHERE id = ? AND deleted = 0",
            (project_id,),
        ).fetchone()
        if project is None:
            return None
        rows = conn.execute(
            """
            SELECT * FROM workspace_project_links
            WHERE project_id = ?
            ORDER BY created_at DESC
            """,
            (project_id,),
        ).fetchall()
        return [_row_to_link(row) for row in rows]
    finally:
        conn.close()


def context_candidates(prompt_lower: str, *, limit: int = 2) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT p.*
            FROM workspace_projects p
            WHERE p.deleted = 0
            ORDER BY p.pinned DESC, length(p.title) DESC, p.updated_at DESC
            LIMIT 100
            """
        ).fetchall()
        matches: list[dict] = []
        for row in rows:
            title = row["title"].strip().lower()
            if title and title in prompt_lower:
                matches.append(_row_to_project(conn, row))
            if len(matches) >= limit:
                break
        return matches
    finally:
        conn.close()


def _project_params(project: dict) -> dict:
    return {
        **project,
        "pinned": 1 if project.get("pinned") else 0,
        "archived": 1 if project.get("archived") else 0,
        "deleted": 1 if project.get("deleted") else 0,
    }


def _replace_tags(conn: sqlite3.Connection, project_id: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM workspace_project_tags WHERE project_id = ?", (project_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO workspace_project_tags(project_id, tag) VALUES (?, ?)",
        [(project_id, tag) for tag in tags],
    )


def _row_to_project(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    data["pinned"] = bool(data["pinned"])
    data["archived"] = bool(data["archived"])
    data["deleted"] = bool(data["deleted"])
    tag_rows = conn.execute(
        "SELECT tag FROM workspace_project_tags WHERE project_id = ? ORDER BY tag ASC",
        (data["id"],),
    ).fetchall()
    data["tags"] = [tag_row["tag"] for tag_row in tag_rows]
    data["linked_notes_count"] = int(data.get("linked_notes_count") or 0)
    return data


def _row_to_note_with_tags(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
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
    data["tags"] = [tag_row["tag"] for tag_row in tag_rows]
    return data


def _row_to_link(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_metadata = data.pop("metadata_json", None)
    data["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    return data
