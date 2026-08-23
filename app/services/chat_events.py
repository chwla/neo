"""The live log for one chat, tailed as NDJSON.

A unified thread mixes two kinds of turn -- a plain reply produced by the chat
generation worker, and an agent run produced by ``agent_core.loop`` -- and the
browser needs one cursor over both.  Both producers append here, so a single
reconnecting reader replaces the old 250ms generation poll and the per-session
event tail.

The vocabulary is ``agent_core.events`` verbatim.  That module already kept the
chat kinds (``chunk``, ``thinking``, ``run.status``) so the renderers could be
shared; using one set of names in one log is the rest of that idea.

The table itself is declared by the ``ChatEvent`` ORM model so ``create_all``
builds it, but access is raw ``sqlite3`` in the convention of Neo's service
stores: the writers are worker threads, and one connection per call keeps them
free of a request-scoped session.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings


def _db_path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def append(
    chat_id: int,
    event_type: str,
    payload: dict | None = None,
    *,
    message_id: int | None = None,
    generation_id: str | None = None,
    agent_session_id: str | None = None,
) -> int:
    """Append one event and return its sequence number.

    A failure here must never take a turn down with it: the log is how the
    browser watches a run, not where the run's own state lives.  The generation
    row and the session row remain the durable record, so a lost event costs a
    reader one refresh rather than the work itself.
    """

    try:
        conn = _connect()
    except sqlite3.Error:
        return 0
    try:
        cursor = conn.execute(
            "INSERT INTO chat_events"
            " (chat_id, generation_id, agent_session_id, message_id, event_type,"
            "  payload_json, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                int(chat_id),
                generation_id,
                agent_session_id,
                message_id,
                event_type,
                json.dumps(payload or {}, default=str),
                _now_iso(),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def list_events(chat_id: int, after: int = 0, limit: int = 500) -> list[dict]:
    """Events after ``after``, oldest first.

    The payload is flattened onto the envelope exactly as
    ``agent_core.store.list_events`` does it, so one frontend reducer reads both
    logs without knowing which produced a record.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chat_events WHERE chat_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (int(chat_id), after, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # A profile database that predates the unified thread has no table yet;
        # it gains one on the next startup rather than failing the read.
        return []
    finally:
        conn.close()
    return [
        {
            "seq": row["seq"],
            "type": row["event_type"],
            "created_at": row["created_at"],
            "message_id": row["message_id"],
            "generation_id": row["generation_id"],
            "agent_session_id": row["agent_session_id"],
            **_loads(row["payload_json"], {}),
        }
        for row in rows
    ]


def first_seq_for(
    chat_id: int,
    *,
    generation_id: str | None = None,
    agent_session_id: str | None = None,
) -> int:
    """The first sequence number belonging to one turn, or 0 when it has none."""

    if generation_id:
        column, value = "generation_id", generation_id
    elif agent_session_id:
        column, value = "agent_session_id", agent_session_id
    else:
        return 0
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT COALESCE(MIN(seq), 0) AS seq FROM chat_events"
            f" WHERE chat_id = ? AND {column} = ?",
            (int(chat_id), value),
        ).fetchone()
        return int(row["seq"]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def latest_seq(chat_id: int) -> int:
    """The newest sequence number in a chat, or 0 when it has no events."""

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM chat_events WHERE chat_id = ?",
            (int(chat_id),),
        ).fetchone()
        return int(row["seq"]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def has_active_turn(chat_id: int) -> bool:
    """Whether anything is still working in this chat.

    A tail closes when the answer is "no": unlike a run, a chat has no terminal
    state of its own, so "nothing is generating" is what stands in for one.

    Both tables are read directly rather than through their own layers because
    the caller is a streaming generator -- holding a request-scoped ORM session
    open for the life of a tail is the cost this avoids.
    """

    conn = _connect()
    try:
        generation = conn.execute(
            "SELECT 1 FROM chat_generations WHERE chat_id = ?"
            " AND status IN ('queued', 'running') LIMIT 1",
            (int(chat_id),),
        ).fetchone()
        if generation:
            return True
    except sqlite3.OperationalError:
        pass
    try:
        session = conn.execute(
            "SELECT 1 FROM workspace_agent_sessions WHERE chat_id = ?"
            " AND status IN ('queued', 'running', 'waiting_approval') LIMIT 1",
            (int(chat_id),),
        ).fetchone()
        return bool(session)
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


__all__ = [
    "append",
    "first_seq_for",
    "has_active_turn",
    "latest_seq",
    "list_events",
]
