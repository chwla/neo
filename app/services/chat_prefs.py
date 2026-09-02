"""Per-profile chat preferences.

The convention is the gallery's (``gallery/store.py``): a small key/value table
in the profile database, read and written with raw ``sqlite3``, defaulted from a
``NEO_`` setting when nothing has been stored.  A separate table rather than a
shared one because a chat concern reaching into ``gallery_preferences`` is the
kind of coupling that stays invisible until someone removes the gallery.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings

MAX_CONCURRENT_TURNS_KEY = "max_concurrent_turns"

#: The bounds the API and the stored value are both clamped to. One, because a
#: cap of zero would accept turns that never run; ten, because the cap exists to
#: stop a local model server being asked for more than it can do at once.
MIN_CONCURRENT_TURNS = 1
MAX_CONCURRENT_TURNS = 10


def _db_path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def initialize_chat_preference_tables() -> None:
    """Create the preferences table for the active profile."""

    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_preference(key: str) -> str | None:
    """The stored value for a chat preference, or None if never set."""

    conn = _connect()
    try:
        row = conn.execute("SELECT value FROM chat_preferences WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    except sqlite3.OperationalError:
        # A profile database that predates this table has no preference yet;
        # the caller falls back to the setting rather than failing the read.
        return None
    finally:
        conn.close()


def set_preference(key: str, value: str) -> None:
    conn = _connect()
    try:
        initialize_chat_preference_tables()
        conn.execute(
            """INSERT INTO chat_preferences (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _clamp(value: int) -> int:
    return max(MIN_CONCURRENT_TURNS, min(MAX_CONCURRENT_TURNS, value))


def max_concurrent_turns() -> int:
    """How many turns may run at once in this profile.

    Called on every admission decision while a lock is held, so it must be cheap
    and must never raise: a preference that cannot be read falls back to the
    deployment default rather than taking the send down with it.
    """

    stored = get_preference(MAX_CONCURRENT_TURNS_KEY)
    if stored is not None:
        try:
            return _clamp(int(stored))
        except (TypeError, ValueError):
            pass
    try:
        return _clamp(int(get_settings().max_concurrent_turns))
    except (TypeError, ValueError, AttributeError):
        return 3


def set_max_concurrent_turns(value: int) -> int:
    clamped = _clamp(int(value))
    set_preference(MAX_CONCURRENT_TURNS_KEY, str(clamped))
    return clamped


__all__ = [
    "MAX_CONCURRENT_TURNS",
    "MAX_CONCURRENT_TURNS_KEY",
    "MIN_CONCURRENT_TURNS",
    "get_preference",
    "initialize_chat_preference_tables",
    "max_concurrent_turns",
    "set_max_concurrent_turns",
    "set_preference",
]
