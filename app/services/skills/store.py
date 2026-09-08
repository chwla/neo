"""The installed-skill registry for the active profile.

The convention is ``rules/store.py``'s: a table in the profile database, read
and written with raw ``sqlite3``, created on demand. A skill's *text* lives on
disk (see ``library.py``); this table records that it exists, what it is called,
and whether new chats start with it on.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_skill_tables() -> None:
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            directory TEXT NOT NULL,
            enabled_by_default INTEGER NOT NULL DEFAULT 1,
            source_type TEXT NOT NULL DEFAULT 'ui',
            source_ref TEXT,
            content_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_skills_slug ON skills(slug);
        """)


def _skill(row) -> dict:
    item = dict(row)
    item["enabled_by_default"] = bool(item["enabled_by_default"])
    return item


def insert_skill(item: dict) -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO skills
            (id, slug, name, description, directory, enabled_by_default,
             source_type, source_ref, content_hash, created_at, updated_at)
            VALUES (:id, :slug, :name, :description, :directory, :enabled_by_default,
                    :source_type, :source_ref, :content_hash, :created_at, :updated_at)""",
            {**item, "enabled_by_default": int(item.get("enabled_by_default", True))},
        )
    return get_skill(item["id"])


def get_skill(skill_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
    return _skill(row) if row else None


def get_skill_by_slug(slug: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM skills WHERE slug=?", (slug,)).fetchone()
    return _skill(row) if row else None


def list_skills() -> list[dict]:
    """Every installed skill, ordered by name.

    Read on every agent turn to resolve the enabled set, so it must never raise:
    a profile database that predates this table has no skills, which is exactly
    what an empty list says.
    """

    try:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM skills ORDER BY name, slug").fetchall()
    except sqlite3.OperationalError:
        return []
    return [_skill(row) for row in rows]


def update_skill(skill_id: str, updates: dict) -> dict | None:
    allowed = {"name", "description", "enabled_by_default", "content_hash", "updated_at"}
    columns, values = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        columns.append(f"{key}=?")
        values.append(int(value) if key == "enabled_by_default" else value)
    if columns:
        with _connect() as conn:
            conn.execute(f"UPDATE skills SET {', '.join(columns)} WHERE id=?", [*values, skill_id])
    return get_skill(skill_id)


def delete_skill(skill_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM skills WHERE id=?", (skill_id,))


__all__ = [
    "delete_skill",
    "get_skill",
    "get_skill_by_slug",
    "initialize_skill_tables",
    "insert_skill",
    "list_skills",
    "now_iso",
    "update_skill",
]
