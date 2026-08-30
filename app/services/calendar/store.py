"""SQLite persistence for calendar events and reminder deliveries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

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
    return datetime.now(UTC).isoformat()


def initialize_calendar_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_events (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                location TEXT NOT NULL DEFAULT '',
                start_at TEXT NOT NULL,
                end_at TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                timezone TEXT NOT NULL DEFAULT 'UTC',
                recurrence_json TEXT,
                reminder_minutes_before_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT 'user',
                created_via_json TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_reminder_deliveries (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL,
                occurrence_start TEXT NOT NULL,
                offset_minutes INTEGER NOT NULL,
                delivered_at TEXT NOT NULL,
                acknowledged_at TEXT,
                FOREIGN KEY (event_id) REFERENCES calendar_events(id),
                UNIQUE (event_id, occurrence_start, offset_minutes)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_events_visible "
            "ON calendar_events(deleted, start_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_reminder_deliveries_event "
            "ON calendar_reminder_deliveries(event_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_calendar_reminder_deliveries_pending "
            "ON calendar_reminder_deliveries(acknowledged_at)"
        )
        conn.commit()
    finally:
        conn.close()


def _row_to_event(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "location": row["location"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "all_day": bool(row["all_day"]),
        "timezone": row["timezone"],
        "recurrence_json": row["recurrence_json"],
        "reminder_minutes_before_json": row["reminder_minutes_before_json"],
        "source": row["source"],
        "created_via_json": row["created_via_json"],
        "deleted": bool(row["deleted"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def insert_event(event: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO calendar_events (
                id, title, description, location, start_at, end_at, all_day, timezone,
                recurrence_json, reminder_minutes_before_json, source, created_via_json,
                deleted, created_at, updated_at
            ) VALUES (
                :id, :title, :description, :location, :start_at, :end_at, :all_day, :timezone,
                :recurrence_json, :reminder_minutes_before_json, :source, :created_via_json,
                :deleted, :created_at, :updated_at
            )
            """,
            {**event, "all_day": 1 if event["all_day"] else 0, "deleted": 0},
        )
        conn.commit()
        return get_event(event["id"], include_deleted=True) or event
    finally:
        conn.close()


def get_event(event_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM calendar_events WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted = 0"
        row = conn.execute(sql, (event_id,)).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


def list_events_starting_before(end: str) -> list[dict]:
    """A safe superset for a [start, end) range query.

    An occurrence of a series can never precede the series' own ``start_at``,
    so any event (recurring or not) that could produce an occurrence inside
    the window necessarily has ``start_at <= end``. Exact filtering against
    the window's lower bound, and recurrence expansion, happen in the service
    layer where the range logic and the ``dateutil`` expansion already live.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE deleted = 0 AND start_at <= ? "
            "ORDER BY start_at ASC",
            (end,),
        ).fetchall()
        return [_row_to_event(row) for row in rows]
    finally:
        conn.close()


def list_all_active_events() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM calendar_events WHERE deleted = 0 ORDER BY start_at ASC"
        ).fetchall()
        return [_row_to_event(row) for row in rows]
    finally:
        conn.close()


def update_event(event_id: str, updates: dict) -> dict | None:
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM calendar_events WHERE id = ? AND deleted = 0", (event_id,)
        ).fetchone()
        if existing is None:
            return None
        columns: list[str] = []
        params: list = []
        for key in (
            "title",
            "description",
            "location",
            "start_at",
            "end_at",
            "timezone",
            "recurrence_json",
            "reminder_minutes_before_json",
        ):
            if key in updates:
                columns.append(f"{key} = ?")
                params.append(updates[key])
        if "all_day" in updates:
            columns.append("all_day = ?")
            params.append(1 if updates["all_day"] else 0)
        if "deleted" in updates:
            columns.append("deleted = ?")
            params.append(1 if updates["deleted"] else 0)
        columns.append("updated_at = ?")
        params.append(updates.get("updated_at") or now_iso())
        params.append(event_id)
        conn.execute(f"UPDATE calendar_events SET {', '.join(columns)} WHERE id = ?", params)
        conn.commit()
        row = conn.execute("SELECT * FROM calendar_events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None
    finally:
        conn.close()


def _row_to_delivery(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "event_id": row["event_id"],
        "occurrence_start": row["occurrence_start"],
        "offset_minutes": row["offset_minutes"],
        "delivered_at": row["delivered_at"],
        "acknowledged_at": row["acknowledged_at"],
    }


def delivery_exists(event_id: str, occurrence_start: str, offset_minutes: int) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM calendar_reminder_deliveries "
            "WHERE event_id = ? AND occurrence_start = ? AND offset_minutes = ?",
            (event_id, occurrence_start, offset_minutes),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def insert_reminder_delivery(delivery: dict) -> dict | None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO calendar_reminder_deliveries (
                id, event_id, occurrence_start, offset_minutes, delivered_at, acknowledged_at
            ) VALUES (:id, :event_id, :occurrence_start, :offset_minutes, :delivered_at, NULL)
            """,
            delivery,
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM calendar_reminder_deliveries WHERE id = ?", (delivery["id"],)
        ).fetchone()
        return _row_to_delivery(row) if row else None
    finally:
        conn.close()


def list_pending_reminder_deliveries() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT d.*, e.title AS event_title FROM calendar_reminder_deliveries d
            JOIN calendar_events e ON e.id = d.event_id
            WHERE d.acknowledged_at IS NULL AND e.deleted = 0
            ORDER BY d.delivered_at ASC
        """).fetchall()
        return [{**_row_to_delivery(row), "event_title": row["event_title"]} for row in rows]
    finally:
        conn.close()


def acknowledge_reminder_delivery(delivery_id: str) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE calendar_reminder_deliveries SET acknowledged_at = ? "
            "WHERE id = ? AND acknowledged_at IS NULL",
            (now_iso(), delivery_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
