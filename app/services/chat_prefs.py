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
MAX_CONCURRENT_EXTERNAL_TURNS_KEY = "max_concurrent_external_turns"
EXTERNAL_AGENTS_ENABLED_KEY = "external_agents_enabled"

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


def max_concurrent_external_turns() -> int:
    """How many external-executor turns may run at once in this profile.

    Same contract as ``max_concurrent_turns``: read under the admission lock, so
    cheap and never-raising. Clamped to at least one -- a zero here would not
    throttle external runs, it would make them unreachable, which is what
    disabling the feature is for.
    """

    stored = get_preference(MAX_CONCURRENT_EXTERNAL_TURNS_KEY)
    if stored is not None:
        try:
            return max(1, int(stored))
        except (TypeError, ValueError):
            pass
    try:
        return max(1, int(get_settings().max_concurrent_external_turns))
    except (TypeError, ValueError, AttributeError):
        return 1


def external_agents_enabled() -> bool:
    """Whether this profile may run external executors at all.

    The setting is still the default, so a deployment can ship the feature off
    (or force it on) without a database. What changed is that "off" is no longer
    a dead end: turning it on is a decision the profile can make for itself and
    Neo can record, rather than an environment variable the person looking at a
    greyed-out menu entry has no way to discover.

    The choice is stored per profile because the privilege is per profile -- one
    account opting into credentialed CLI runs must not opt in every other
    account sharing the installation.
    """

    stored = get_preference(EXTERNAL_AGENTS_ENABLED_KEY)
    if stored is not None:
        return stored.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(get_settings().external_agents_enabled)
    except AttributeError:
        return False


def set_external_agents_enabled(value: bool) -> bool:
    """Record the profile's choice, and return what it now is."""

    set_preference(EXTERNAL_AGENTS_ENABLED_KEY, "1" if value else "0")
    return bool(value)


def set_max_concurrent_external_turns(value: int) -> int:
    clamped = max(1, min(5, int(value)))
    set_preference(MAX_CONCURRENT_EXTERNAL_TURNS_KEY, str(clamped))
    return clamped


def set_max_concurrent_turns(value: int) -> int:
    clamped = _clamp(int(value))
    set_preference(MAX_CONCURRENT_TURNS_KEY, str(clamped))
    return clamped


__all__ = [
    "EXTERNAL_AGENTS_ENABLED_KEY",
    "MAX_CONCURRENT_EXTERNAL_TURNS_KEY",
    "MAX_CONCURRENT_TURNS",
    "MAX_CONCURRENT_TURNS_KEY",
    "MIN_CONCURRENT_TURNS",
    "external_agents_enabled",
    "get_preference",
    "initialize_chat_preference_tables",
    "max_concurrent_turns",
    "set_external_agents_enabled",
    "set_max_concurrent_turns",
    "set_preference",
]
