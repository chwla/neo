"""Per-profile keyboard bindings.

Two tables, following the convention ``chat_prefs`` states: a small key/value
table for the settings, and a separate table for the bindings themselves, both in
the profile database and both read with raw ``sqlite3``.  A concern of its own
rather than a row in ``chat_preferences``, for the reason given there -- the
coupling only becomes visible when somebody removes the other feature.

Bindings are stored as **overrides only**.  A command the user has never touched
has no row, and its binding comes from the catalogue in the browser.  That
matters more than it looks: with the whole map stored, "reset this to the
default" could not tell *never changed* from *changed to what the default happens
to be today*, and a later release that ships a better default would never reach
anybody.  Absence is the default, so it does.

The other deliberate decision is that nothing here knows what a command is.  Ids
are stored as given and are never validated against a catalogue, which is what
keeps "adding a command is one object literal in one file" literally true.  A
stale row left behind by an upgrade is harmless; the browser ignores ids it does
not recognise when it merges.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings

#: The two slots a command's keys live in.  "primary" is the modifier chord,
#: "alternate" the fast single key or sequence.  Both are always bound -- neither
#: is a mode -- and they are separate rows so either can be rebound on its own.
KEYMAPS = ("primary", "alternate")

SEQUENCE_TIMEOUT_MS_KEY = "sequence_timeout_ms"

#: How long a half-typed sequence waits for its next chord.  Nine hundred
#: milliseconds because a sequence that waits a full second reads as a hang, and
#: one that waits much less is unusable for anybody who does not touch-type.
DEFAULT_SEQUENCE_TIMEOUT_MS = 900
MIN_SEQUENCE_TIMEOUT_MS = 200
MAX_SEQUENCE_TIMEOUT_MS = 5000

#: Long enough for a three-chord sequence with modifiers on each, and short
#: enough that a row cannot be used to store something that is not a binding.
MAX_SEQUENCE_LENGTH = 60
MAX_COMMAND_ID_LENGTH = 100

#: Characters a canonical chord can contain: letters, digits, the modifier
#: separator, the space between chords, and the punctuation keys that are bindable.
_SEQUENCE_PATTERN = re.compile(r"^[A-Za-z0-9+ ._,/\\\[\]?<>=;'`~!@#$%^&*()|:\"-]*$")


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


def _now() -> str:
    return datetime.now(UTC).isoformat()


def initialize_keybinding_tables() -> None:
    """Create both tables for the active profile."""

    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keyboard_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS keybinding_overrides (
                command_id TEXT NOT NULL,
                keymap TEXT NOT NULL,
                sequence TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (command_id, keymap)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def get_preference(key: str) -> str | None:
    """The stored value for a keyboard preference, or None if never set."""

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM keyboard_preferences WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    except sqlite3.OperationalError:
        # A profile database that predates these tables has no preference yet;
        # the caller falls back to the default rather than failing the read.
        return None
    finally:
        conn.close()


def set_preference(key: str, value: str) -> None:
    conn = _connect()
    try:
        initialize_keybinding_tables()
        conn.execute(
            """INSERT INTO keyboard_preferences (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _clamp_timeout(value: int) -> int:
    return max(MIN_SEQUENCE_TIMEOUT_MS, min(MAX_SEQUENCE_TIMEOUT_MS, value))


def sequence_timeout_ms() -> int:
    """How long a half-typed sequence waits.  Never raises; clamps what it finds."""

    stored = get_preference(SEQUENCE_TIMEOUT_MS_KEY)
    if stored is not None:
        try:
            return _clamp_timeout(int(stored))
        except (TypeError, ValueError):
            pass
    return DEFAULT_SEQUENCE_TIMEOUT_MS


def set_sequence_timeout_ms(value: int) -> int:
    clamped = _clamp_timeout(int(value))
    set_preference(SEQUENCE_TIMEOUT_MS_KEY, str(clamped))
    return clamped


def _validate(command_id: str, keymap: str, sequence: str) -> tuple[str, str, str]:
    command_id = (command_id or "").strip()
    sequence = (sequence or "").strip()

    if keymap not in KEYMAPS:
        raise ValueError(f"Unknown key slot: {keymap}.")
    if not command_id or len(command_id) > MAX_COMMAND_ID_LENGTH:
        raise ValueError("A command id must be present and at most 100 characters.")
    if len(sequence) > MAX_SEQUENCE_LENGTH:
        raise ValueError("A key sequence must be at most 60 characters.")
    if not _SEQUENCE_PATTERN.match(sequence):
        raise ValueError("That key sequence contains characters a chord cannot hold.")
    return command_id, keymap, sequence


def list_overrides() -> list[dict]:
    """Every binding this profile has changed.  Empty for a profile that has not."""

    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT command_id, keymap, sequence FROM keybinding_overrides
               ORDER BY keymap, command_id"""
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def set_override(command_id: str, keymap: str, sequence: str) -> dict:
    """Bind one command in one keymap.  An empty sequence unbinds it deliberately."""

    command_id, keymap, sequence = _validate(command_id, keymap, sequence)
    conn = _connect()
    try:
        initialize_keybinding_tables()
        conn.execute(
            """INSERT INTO keybinding_overrides (command_id, keymap, sequence, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(command_id, keymap) DO UPDATE SET sequence = excluded.sequence,
                                                             updated_at = excluded.updated_at""",
            (command_id, keymap, sequence, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"command_id": command_id, "keymap": keymap, "sequence": sequence}


def clear_override(command_id: str, keymap: str) -> None:
    """Put one command back to its shipped binding by forgetting the row."""

    command_id, keymap, _ = _validate(command_id, keymap, "")
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM keybinding_overrides WHERE command_id = ? AND keymap = ?",
            (command_id, keymap),
        )
        conn.commit()
    except sqlite3.OperationalError:
        # Nothing was ever stored, so there is nothing to put back.
        pass
    finally:
        conn.close()


def clear_all_overrides() -> None:
    """Put every binding back.  Leaves the settings alone -- they are not bindings."""

    conn = _connect()
    try:
        conn.execute("DELETE FROM keybinding_overrides")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def config() -> dict:
    """Everything the browser needs to build its keymaps, in one read."""

    return {
        "sequence_timeout_ms": sequence_timeout_ms(),
        "overrides": list_overrides(),
    }
