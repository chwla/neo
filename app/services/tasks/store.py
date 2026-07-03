"""SQLite persistence for the Tasks v1 workspace."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.core.config import get_settings
from app.services.notes.store import get_note
from app.services.projects.store import get_project


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


def initialize_task_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'todo',
                priority TEXT NOT NULL DEFAULT 'medium',
                due_at TEXT,
                project_id TEXT,
                pinned INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES workspace_projects(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_task_tags (
                task_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (task_id, tag),
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_task_notes (
                task_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (task_id, note_id),
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id),
                FOREIGN KEY (note_id) REFERENCES notes(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_task_links (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                target_id TEXT,
                target_url TEXT,
                title TEXT,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES workspace_tasks(id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_tasks_visible ON workspace_tasks(deleted, archived, status, priority, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_tasks_project ON workspace_tasks(project_id, deleted, archived, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_tasks_due ON workspace_tasks(due_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_task_tags_tag ON workspace_task_tags(tag)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_task_notes_task ON workspace_task_notes(task_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_task_notes_note ON workspace_task_notes(note_id)")
        conn.commit()
    finally:
        conn.close()


def insert_task(task: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tasks (
                id, title, description, status, priority, due_at, project_id,
                pinned, archived, deleted, completed_at, created_at, updated_at
            ) VALUES (
                :id, :title, :description, :status, :priority, :due_at, :project_id,
                :pinned, :archived, :deleted, :completed_at, :created_at, :updated_at
            )
            """,
            _task_params(task),
        )
        _replace_tags(conn, task["id"], task.get("tags", []))
        conn.commit()
        return get_task(task["id"], include_deleted=True) or task
    finally:
        conn.close()


def get_task(task_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT t.*, p.title AS project_title FROM workspace_tasks t LEFT JOIN workspace_projects p ON p.id = t.project_id WHERE t.id = ?"
        if not include_deleted:
            sql += " AND t.deleted = 0"
        row = conn.execute(sql, (task_id,)).fetchone()
        return _row_to_task(conn, row) if row else None
    finally:
        conn.close()


def list_tasks(
    *,
    q: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    project_id: str | None = None,
    tag: str | None = None,
    due_before: str | None = None,
    due_after: str | None = None,
    include_archived: bool = False,
    include_done: bool = True,
    pinned_first: bool = True,
    sort_mode: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        joins = " LEFT JOIN workspace_projects p ON p.id = t.project_id"
        where = ["t.deleted = 0"]
        params: list = []
        if not include_archived:
            where.append("t.archived = 0")
        if not include_done:
            where.append("t.status != 'done'")
        if status:
            where.append("t.status = ?")
            params.append(status)
        if priority:
            where.append("t.priority = ?")
            params.append(priority)
        if project_id:
            where.append("t.project_id = ?")
            params.append(project_id)
        if tag:
            joins += " JOIN workspace_task_tags ft ON ft.task_id = t.id"
            where.append("ft.tag = ?")
            params.append(tag)
        if due_before:
            where.append("t.due_at IS NOT NULL AND t.due_at <= ?")
            params.append(due_before)
        if due_after:
            where.append("t.due_at IS NOT NULL AND t.due_at >= ?")
            params.append(due_after)
        if q:
            like = f"%{q.lower()}%"
            where.append("""
                (lower(t.title) LIKE ? OR lower(t.description) LIKE ?
                 OR lower(COALESCE(p.title, '')) LIKE ?
                 OR EXISTS (SELECT 1 FROM workspace_task_tags st WHERE st.task_id = t.id AND lower(st.tag) LIKE ?))
            """)
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)
        pin_sort = "t.pinned DESC, " if pinned_first else ""
        if sort_mode == "completed_recent":
            order_sql = f"{pin_sort}CASE WHEN t.completed_at IS NULL THEN 1 ELSE 0 END, t.completed_at DESC, t.updated_at DESC"
        elif sort_mode == "due_soon":
            order_sql = (
                f"{pin_sort}CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END, t.due_at ASC, "
                "CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.updated_at DESC"
            )
        else:
            order_sql = (
                f"{pin_sort}"
                "CASE t.status WHEN 'doing' THEN 0 WHEN 'blocked' THEN 1 WHEN 'todo' THEN 2 WHEN 'done' THEN 3 ELSE 4 END, "
                "CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
                "CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END, t.due_at ASC, t.updated_at DESC"
            )
        total = conn.execute(
            f"SELECT COUNT(DISTINCT t.id) FROM workspace_tasks t{joins} WHERE {where_sql}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT DISTINCT t.*, p.title AS project_title,
                (SELECT COUNT(*) FROM workspace_task_notes tn JOIN notes n ON n.id = tn.note_id
                 WHERE tn.task_id = t.id AND n.deleted = 0) AS linked_notes_count
            FROM workspace_tasks t{joins}
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_task(conn, row) for row in rows], int(total)
    finally:
        conn.close()


def update_task(task_id: str, updates: dict) -> dict | None:
    conn = _connect()
    try:
        existing = conn.execute("SELECT id FROM workspace_tasks WHERE id = ? AND deleted = 0", (task_id,)).fetchone()
        if existing is None:
            return None
        columns: list[str] = []
        params: list = []
        for key in ("title", "description", "status", "priority", "due_at", "project_id", "completed_at"):
            if key in updates:
                columns.append(f"{key} = ?")
                params.append(updates[key])
        for key in ("pinned", "archived", "deleted"):
            if key in updates:
                columns.append(f"{key} = ?")
                params.append(1 if updates[key] else 0)
        columns.append("updated_at = ?")
        params.append(updates.get("updated_at") or now_iso())
        params.append(task_id)
        conn.execute(f"UPDATE workspace_tasks SET {', '.join(columns)} WHERE id = ?", params)
        if "tags" in updates:
            _replace_tags(conn, task_id, updates["tags"])
        conn.commit()
        row = conn.execute(
            "SELECT t.*, p.title AS project_title FROM workspace_tasks t LEFT JOIN workspace_projects p ON p.id = t.project_id WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        return _row_to_task(conn, row)
    finally:
        conn.close()


def list_tags() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT tt.tag, COUNT(*) AS count FROM workspace_task_tags tt
            JOIN workspace_tasks t ON t.id = tt.task_id
            WHERE t.deleted = 0 GROUP BY tt.tag ORDER BY count DESC, tt.tag ASC
        """).fetchall()
        return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


def attach_note(task_id: str, note_id: str) -> bool:
    conn = _connect()
    try:
        task = conn.execute("SELECT id FROM workspace_tasks WHERE id = ? AND deleted = 0", (task_id,)).fetchone()
        if task is None or get_note(note_id) is None:
            return False
        now = now_iso()
        conn.execute("INSERT OR IGNORE INTO workspace_task_notes(task_id, note_id, created_at) VALUES (?, ?, ?)", (task_id, note_id, now))
        conn.execute("UPDATE workspace_tasks SET updated_at = ? WHERE id = ?", (now, task_id))
        conn.commit()
        return True
    finally:
        conn.close()


def detach_note(task_id: str, note_id: str) -> bool:
    conn = _connect()
    try:
        task = conn.execute("SELECT id FROM workspace_tasks WHERE id = ? AND deleted = 0", (task_id,)).fetchone()
        if task is None:
            return False
        conn.execute("DELETE FROM workspace_task_notes WHERE task_id = ? AND note_id = ?", (task_id, note_id))
        conn.execute("UPDATE workspace_tasks SET updated_at = ? WHERE id = ?", (now_iso(), task_id))
        conn.commit()
        return True
    finally:
        conn.close()


def list_task_notes(task_id: str) -> list[dict] | None:
    conn = _connect()
    try:
        task = conn.execute("SELECT id FROM workspace_tasks WHERE id = ? AND deleted = 0", (task_id,)).fetchone()
        if task is None:
            return None
        rows = conn.execute("""
            SELECT n.*, tn.created_at AS attached_at FROM workspace_task_notes tn
            JOIN notes n ON n.id = tn.note_id
            WHERE tn.task_id = ? AND n.deleted = 0 ORDER BY n.updated_at DESC
        """, (task_id,)).fetchall()
        return [_row_to_note_with_tags(conn, row) for row in rows]
    finally:
        conn.close()


def list_note_tasks(note_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT t.*, p.title AS project_title FROM workspace_task_notes tn
            JOIN workspace_tasks t ON t.id = tn.task_id
            LEFT JOIN workspace_projects p ON p.id = t.project_id
            WHERE tn.note_id = ? AND t.deleted = 0
            ORDER BY t.pinned DESC, t.updated_at DESC
        """, (note_id,)).fetchall()
        return [_row_to_task(conn, row) for row in rows]
    finally:
        conn.close()


def list_links(task_id: str) -> list[dict] | None:
    conn = _connect()
    try:
        if conn.execute("SELECT id FROM workspace_tasks WHERE id = ? AND deleted = 0", (task_id,)).fetchone() is None:
            return None
        rows = conn.execute("SELECT * FROM workspace_task_links WHERE task_id = ? ORDER BY created_at DESC", (task_id,)).fetchall()
        return [_row_to_link(row) for row in rows]
    finally:
        conn.close()


def _task_params(task: dict) -> dict:
    return {**task, "pinned": int(bool(task.get("pinned"))), "archived": int(bool(task.get("archived"))), "deleted": int(bool(task.get("deleted")))}


def _replace_tags(conn: sqlite3.Connection, task_id: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM workspace_task_tags WHERE task_id = ?", (task_id,))
    conn.executemany("INSERT OR IGNORE INTO workspace_task_tags(task_id, tag) VALUES (?, ?)", [(task_id, tag) for tag in tags])


def _row_to_task(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("pinned", "archived", "deleted"):
        data[key] = bool(data[key])
    tags = conn.execute("SELECT tag FROM workspace_task_tags WHERE task_id = ? ORDER BY tag ASC", (data["id"],)).fetchall()
    data["tags"] = [item["tag"] for item in tags]
    data["linked_notes_count"] = int(data.get("linked_notes_count") or 0)
    return data


def _row_to_note_with_tags(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_metadata = data.pop("source_metadata_json", None)
    data["source_metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    for key in ("pinned", "archived", "deleted"):
        data[key] = bool(data[key])
    tags = conn.execute("SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag ASC", (data["id"],)).fetchall()
    data["tags"] = [item["tag"] for item in tags]
    return data


def _row_to_link(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_metadata = data.pop("metadata_json", None)
    data["metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    return data
