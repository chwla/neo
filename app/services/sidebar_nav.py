"""Per-profile sidebar navigation: which SYSTEM entries this profile keeps.

The convention is ``appearance.py``'s and ``chat_prefs.py``'s -- a small
key/value table in the profile database, read and written with raw ``sqlite3``
-- and a table of its own for the reason recorded there: a concern reaching
into another feature's preferences is the kind of coupling that stays invisible
until someone removes that feature. Which places the sidebar offers is not how
Neo looks, so it does not live in ``appearance_preferences``.

The profile database is what makes this per-profile without any code here
knowing whose it is: ``ProfileSessionMiddleware`` binds the request to the
signed-in profile's database before a route runs. That is also why it is not
``localStorage``, which is scoped to the origin and would make one person's
sidebar everybody's on a shared machine -- and would not survive the profile
switch that clears origin-scoped state.

**Hidden is stored, not visible.** A stored list of what to show freezes the
menu at the moment it was saved: every entry added to Neo afterwards would be
missing from that list, and so invisible to exactly the people who had bothered
to configure it. Storing what to hide makes "pinned" the default a new entry
inherits. The same reasoning is written out in ``frontend/src/systemNav.js``,
which holds the other copy of these ids.

The id list is duplicated from that module on purpose, the way the theme
catalogue is: the browser needs it to draw the panel and the server needs it to
refuse a value it would otherwise store forever, and a shared source would mean
shipping a constant through an endpoint that exists only to restate it.
``tests/test_sidebar_nav_api.py`` holds the two lists together.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings

HIDDEN_SYSTEM_ITEMS_KEY = "hidden_system_items"

#: Every entry the sidebar's SYSTEM section can draw, in the order it draws
#: them. Mirrors ``SYSTEM_NAV`` in ``frontend/src/systemNav.js``.
VALID_SYSTEM_ITEMS = (
    "memory",
    "research",
    "notes",
    "calendar",
    "gallery",
    "localModels",
    "compareModels",
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


def initialize_sidebar_nav_tables() -> None:
    """Create the preferences table for the active profile."""

    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sidebar_nav_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _read(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM sidebar_nav_preferences WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def get_preference(key: str) -> str | None:
    """The stored value, or None if this profile has never set one.

    A missing table is repaired and re-read rather than reported as "never
    chose". The two are indistinguishable to the caller and they are not the
    same thing: a server process that was started before this table joined the
    initialiser list would otherwise answer with the defaults on every load,
    with nothing logged and nothing to notice, and the only thing that could fix
    it is a write -- because ``set_preference`` creates the table and a read
    never did. That is the exact shape of "I turned Gallery off and it came
    back when I refreshed", which is the bug this whole feature would be
    reported as.
    """

    conn = _connect()
    try:
        try:
            return _read(conn, key)
        except sqlite3.OperationalError:
            initialize_sidebar_nav_tables()
            return _read(conn, key)
    except sqlite3.OperationalError:
        # The table could not be created either -- a read-only or missing
        # database. The caller's default is the only answer left, and a sidebar
        # with every entry in it is the right way to fail.
        return None
    finally:
        conn.close()


def set_preference(key: str, value: str) -> None:
    conn = _connect()
    try:
        initialize_sidebar_nav_tables()
        conn.execute(
            """INSERT INTO sidebar_nav_preferences (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def hidden_system_items() -> list[str]:
    """The SYSTEM entries this profile has turned off.

    Anything unrecognised in the stored row is filtered out of the answer but
    left in the row, so an entry that is renamed or missing from this build does
    not take somebody's choice with it -- putting it back restores the choice,
    the same way a retired theme id does. A row that is not a JSON list at all
    reads as "nothing hidden": the sidebar has to render either way, and a
    complete one is the harmless direction to be wrong in.
    """

    stored = get_preference(HIDDEN_SYSTEM_ITEMS_KEY)
    if not stored:
        return []
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in VALID_SYSTEM_ITEMS if item in parsed]


def set_hidden_system_items(values: list[str]) -> list[str]:
    """Store the entries to hide, rejecting an id the sidebar cannot draw.

    Refused on the way in rather than filtered, because an id this does not
    recognise is a client that disagrees with the catalogue, and storing a
    typo silently would leave it in the row forever looking like a choice.

    Normalised to catalogue order with duplicates dropped, so the stored value
    is a set rather than a transcript of the order the boxes were clicked in --
    two profiles that hid the same things get the same row.
    """

    unknown = [value for value in values if value not in VALID_SYSTEM_ITEMS]
    if unknown:
        raise ValueError(f"unknown sidebar item: {unknown[0]!r}")

    hidden = [item for item in VALID_SYSTEM_ITEMS if item in set(values)]
    set_preference(HIDDEN_SYSTEM_ITEMS_KEY, json.dumps(hidden))
    return hidden


def config() -> dict:
    """What the panel needs to draw itself, and the sidebar to filter itself.

    ``items`` is the catalogue rather than only the visible half: the panel
    draws a row per entry with a toggle, so it needs the ones that are off as
    much as the ones that are on -- and a client can tell an entry this server
    would refuse from one it simply has not been told about.
    """

    return {
        "items": list(VALID_SYSTEM_ITEMS),
        "hidden": hidden_system_items(),
    }


__all__ = [
    "HIDDEN_SYSTEM_ITEMS_KEY",
    "VALID_SYSTEM_ITEMS",
    "config",
    "get_preference",
    "hidden_system_items",
    "initialize_sidebar_nav_tables",
    "set_hidden_system_items",
    "set_preference",
]
