"""Per-profile appearance preferences: which theme this profile is wearing.

The convention is the gallery's and the chat's (``chat_prefs.py``): a small
key/value table in the profile database, read and written with raw ``sqlite3``.
A separate table rather than a shared one, for the reason recorded there -- a
concern reaching into another feature's preferences is the kind of coupling
that stays invisible until someone removes that feature.

The list of valid ids is duplicated from ``frontend/src/themes.js`` on purpose.
The browser needs it to draw the picker and the server needs it to refuse a
value it would otherwise store forever, and a shared source would mean shipping
the palette through an endpoint that exists only to restate a constant. A test
holds the two lists together.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings

THEME_KEY = "theme"

#: The theme a profile that has never chosen one gets, and the value an unknown
#: stored id falls back to. It is also the palette every screen shown before a
#: profile is known renders in, because no ``data-theme`` attribute means this.
DEFAULT_THEME = "default"

#: Every id the stylesheet has a ``[data-theme]`` block for, plus the default.
VALID_THEMES = (
    DEFAULT_THEME,
    "cyberpunk",
    "amber",
    "ice",
    "mono",
    "indigo",
    "crimson",
    "paper",
)


def _db_path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def initialize_appearance_tables() -> None:
    """Create the preferences table for the active profile."""

    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appearance_preferences (
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
    """The stored value for an appearance preference, or None if never set."""

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM appearance_preferences WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except sqlite3.OperationalError:
        # A profile database that predates this table has no preference yet;
        # the caller falls back to the default rather than failing the read.
        return None
    finally:
        conn.close()


def set_preference(key: str, value: str) -> None:
    conn = _connect()
    try:
        initialize_appearance_tables()
        conn.execute(
            """INSERT INTO appearance_preferences (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def theme() -> str:
    """This profile's theme.

    A stored id that is no longer shipped reads as the default rather than as
    itself, so removing a theme leaves every profile that was using it with a
    working interface instead of an unstyled one. The row is left alone: if the
    theme comes back, so does the choice.
    """

    stored = get_preference(THEME_KEY)
    return stored if stored in VALID_THEMES else DEFAULT_THEME


def set_theme(value: str) -> str:
    """Store a theme, rejecting one the stylesheet has no block for."""

    if value not in VALID_THEMES:
        raise ValueError(f"unknown theme: {value!r}")
    set_preference(THEME_KEY, value)
    return value


def config() -> dict:
    """Everything the browser needs to dress itself."""

    return {"theme": theme(), "available": list(VALID_THEMES)}


__all__ = [
    "DEFAULT_THEME",
    "THEME_KEY",
    "VALID_THEMES",
    "config",
    "get_preference",
    "initialize_appearance_tables",
    "set_preference",
    "set_theme",
    "theme",
]
