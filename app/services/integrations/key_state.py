"""What Neo remembers about the connector master key, and what depends on it.

The key itself lives in a file (or the environment); see ``vault``. This module
keeps the facts that decide what to do when that file is missing or different:
the fingerprint of the key last in use, a history of generations, replacements
and resets, and a count of the sealed rows each profile holds.

They live in the profile registry, beside the accounts, because the question
"is a new key safe to mint?" is one about the whole install: a key file that has
vanished while sealed credentials remain must never be quietly replaced, or every
connected account on every profile becomes unreadable with nobody told why.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Sealed under the connector key, per profile database: table -> rows that count.
SEALED_TABLES: dict[str, str] = {
    "integration_credentials": "1",
    "integration_oauth_clients": "secret_ciphertext IS NOT NULL",
    "integration_oauth_states": "used_at IS NULL",
    "workspace_github_credentials": "1",
}


def fingerprint(key: bytes) -> str:
    """A short, non-reversible name for a key, safe to log and to show."""

    return hashlib.sha256(b"neo-connector-key-id:" + key).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _registry() -> sqlite3.Connection:
    from app.services import profile_accounts

    profile_accounts.initialize_profile_registry()
    conn = profile_accounts._connect_registry()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_key_state ("
        " id INTEGER PRIMARY KEY CHECK (id = 1),"
        " fingerprint TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_key_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
        " old_fingerprint TEXT, new_fingerprint TEXT NOT NULL,"
        " sealed_counts_json TEXT NOT NULL DEFAULT '{}', recorded_at TEXT NOT NULL)"
    )
    return conn


def recorded_fingerprint() -> str | None:
    conn = _registry()
    try:
        row = conn.execute("SELECT fingerprint FROM connector_key_state WHERE id = 1").fetchone()
        return str(row["fingerprint"]) if row else None
    finally:
        conn.close()


def record(new: str, *, kind: str, old: str | None = None, counts: dict | None = None) -> None:
    """Make ``new`` the key in use, and keep a line of history saying why."""

    conn = _registry()
    try:
        now = _now()
        conn.execute(
            "INSERT INTO connector_key_state (id, fingerprint, recorded_at) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET fingerprint = excluded.fingerprint,"
            " recorded_at = excluded.recorded_at",
            (new, now),
        )
        conn.execute(
            "INSERT INTO connector_key_events"
            " (kind, old_fingerprint, new_fingerprint, sealed_counts_json, recorded_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (kind, old, new, json.dumps(counts or {}, sort_keys=True), now),
        )
        conn.commit()
    finally:
        conn.close()


def events() -> list[dict]:
    conn = _registry()
    try:
        rows = conn.execute("SELECT * FROM connector_key_events ORDER BY id").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _profile_databases() -> list[tuple[str, Path]]:
    from app.services.profile_accounts import profiles_root

    accounts = profiles_root() / "accounts"
    if not accounts.is_dir():
        return []
    return [
        (entry.name, entry / "neo.db")
        for entry in sorted(accounts.iterdir())
        if (entry / "neo.db").is_file()
    ]


def sealed_counts() -> dict[str, dict[str, int]]:
    """Sealed rows per profile, read-only, omitting profiles that hold none."""

    found: dict[str, dict[str, int]] = {}
    for profile_id, database in _profile_databases():
        try:
            conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30)
        except sqlite3.Error:
            continue
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            counts = {}
            for table, condition in SEALED_TABLES.items():
                if table in tables:
                    count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}")
                    value = int(count.fetchone()[0])
                    if value:
                        counts[table] = value
            if counts:
                found[profile_id] = counts
        except sqlite3.Error:
            _LOG.warning("connector_key_count_failed profile=%s", profile_id, exc_info=True)
        finally:
            conn.close()
    return found


def orphan_token(counts: dict[str, dict[str, int]]) -> str:
    """The reset confirmation when sealed rows exist but no key was ever recorded.

    Derived from what would be destroyed, so it can only be copied from the
    refusal that listed it, and changes if that set changes.
    """

    return hashlib.sha256(
        ("neo-connector-orphans:" + json.dumps(counts, sort_keys=True)).encode()
    ).hexdigest()[:16]


def mark_reconnect_required(profile_ids: list[str]) -> None:
    """Flag every connected account in these profiles as needing to be reconnected."""

    databases = dict(_profile_databases())
    for profile_id in profile_ids:
        database = databases.get(profile_id)
        if database is None:
            continue
        try:
            conn = sqlite3.connect(database, timeout=30)
            try:
                conn.execute(
                    "UPDATE integration_connections SET status = 'reconnect_required',"
                    " last_error_category = 'connector_key_reset', updated_at = ?",
                    (_now(),),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            _LOG.warning("connector_key_mark_failed profile=%s", profile_id, exc_info=True)
