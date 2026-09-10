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
holds the two lists together. The same goes for the chat background, whose
catalogue lives in ``frontend/src/backgrounds/index.js``.

Each preference is its own row, so a write of one never restates another: the
theme and the background are chosen on different screens, and a patch carrying
the whole configuration would let the later of two writes win on every field
rather than on the one it meant to change.
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

BACKGROUND_KEY = "background"

#: Motion behind the transcript is off unless it is asked for. The default has
#: to be the one that mounts no canvas at all, so that a profile which has never
#: chosen pays nothing for the feature existing.
DEFAULT_BACKGROUND = "none"

#: Every id with an effect module in ``frontend/src/backgrounds/``, plus "none".
VALID_BACKGROUNDS = (
    DEFAULT_BACKGROUND,
    "jellyfish",
    "stars",
    "rain",
    "gradient",
)

#: Backgrounds that were replaced rather than removed, and what replaced them.
#:
#: A stored id that is no longer shipped reads as the default, which is correct
#: for a background that is simply gone -- but "waves" was retired in favour of
#: "gradient", another broad field effect, so falling back would silently take
#: motion away from every profile that had chosen it. The choice was for a
#: moving field, and that still exists. Read as the successor and the next write
#: from the picker stores the new id, so a profile migrates by being used.
SUPERSEDED_BACKGROUNDS = {"waves": "gradient"}

INTENSITY_KEY = "background_intensity"

DEFAULT_INTENSITY = "medium"

#: How strongly the background draws itself. Separate from the choice of effect
#: because the readable strength depends on the theme underneath it -- the same
#: alpha that reads as a whisper on Ice is a smear on Paper.
VALID_INTENSITIES = ("subtle", DEFAULT_INTENSITY, "vivid")


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


def _read(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM appearance_preferences WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def get_preference(key: str) -> str | None:
    """The stored value for an appearance preference, or None if never set.

    A missing table is repaired and re-read rather than reported as "never
    chose". The two are indistinguishable to the caller and they are not the
    same thing: a profile whose initialisers have not run in this process yet
    would otherwise be dressed in the defaults on every single load, with
    nothing logged and nothing to notice, and the only thing that could ever fix
    it is a write -- because ``set_preference`` creates the table and a read
    never did. That is a preference that silently refuses to persist, which is
    exactly how this reads to someone who picked a background and refreshed.

    ``ensure_profile_storage`` is memoised per process, so the gap is real:
    a long-running server that was started before this table joined the
    initialiser list never creates it, and every read here returns the default
    forever.
    """

    conn = _connect()
    try:
        try:
            return _read(conn, key)
        except sqlite3.OperationalError:
            initialize_appearance_tables()
            return _read(conn, key)
    except sqlite3.OperationalError:
        # The table could not be created either -- a read-only or missing
        # database. The caller's default is the only answer left, and failing
        # the whole appearance read would leave the interface with no palette.
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


def background() -> str:
    """This profile's chat background.

    Falls back the same way a theme does, and for a sharper reason: the id
    selects a rendering module in the browser, so one that is no longer shipped
    would otherwise leave a mounted canvas with nothing to draw on it.

    An id that was replaced rather than retired reads as its replacement --
    see ``SUPERSEDED_BACKGROUNDS``. The row is left alone either way, so nothing
    is rewritten underneath a profile that has not opened the picker.
    """

    stored = get_preference(BACKGROUND_KEY)
    if stored in VALID_BACKGROUNDS:
        return stored
    return SUPERSEDED_BACKGROUNDS.get(stored, DEFAULT_BACKGROUND)


def set_background(value: str) -> str:
    """Store a background, rejecting one that has no effect module."""

    if value not in VALID_BACKGROUNDS:
        raise ValueError(f"unknown background: {value!r}")
    set_preference(BACKGROUND_KEY, value)
    return value


def intensity() -> str:
    """How strongly this profile's background draws itself."""

    stored = get_preference(INTENSITY_KEY)
    return stored if stored in VALID_INTENSITIES else DEFAULT_INTENSITY


def set_intensity(value: str) -> str:
    """Store an intensity, rejecting one the effects do not know how to scale."""

    if value not in VALID_INTENSITIES:
        raise ValueError(f"unknown intensity: {value!r}")
    set_preference(INTENSITY_KEY, value)
    return value


def config() -> dict:
    """Everything the browser needs to dress itself.

    ``available`` stays the list of themes rather than growing into a map of
    every catalogue: it is already the shape the picker reads, and renaming it
    would break a client that is only ever served from this repository for no
    gain over adding the new lists beside it.
    """

    return {
        "theme": theme(),
        "available": list(VALID_THEMES),
        "background": background(),
        "backgrounds": list(VALID_BACKGROUNDS),
        "intensity": intensity(),
        "intensities": list(VALID_INTENSITIES),
    }


__all__ = [
    "BACKGROUND_KEY",
    "DEFAULT_BACKGROUND",
    "DEFAULT_INTENSITY",
    "DEFAULT_THEME",
    "INTENSITY_KEY",
    "THEME_KEY",
    "VALID_BACKGROUNDS",
    "VALID_INTENSITIES",
    "VALID_THEMES",
    "background",
    "config",
    "get_preference",
    "initialize_appearance_tables",
    "intensity",
    "set_background",
    "set_intensity",
    "set_preference",
    "set_theme",
    "theme",
]
