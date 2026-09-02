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


def _project(row: sqlite3.Row) -> dict:
    """One row as the envelope the browser reads.

    The payload is flattened onto the envelope exactly as
    ``agent_core.store.list_events`` does it, so one frontend reducer reads both
    logs without knowing which produced a record.

    ``chat_id`` is on the envelope because the profile-wide tail interleaves
    every conversation onto one connection and the reader routes by it.  A
    single-chat reader simply ignores a field whose value it already knows.
    """

    return {
        "seq": row["seq"],
        "chat_id": row["chat_id"],
        "type": row["event_type"],
        "created_at": row["created_at"],
        "message_id": row["message_id"],
        "generation_id": row["generation_id"],
        "agent_session_id": row["agent_session_id"],
        **_loads(row["payload_json"], {}),
    }


def list_events(chat_id: int, after: int = 0, limit: int = 500) -> list[dict]:
    """Events after ``after``, oldest first."""

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
    return [_project(row) for row in rows]


def list_all_events(after: int = 0, limit: int = 500) -> list[dict]:
    """Every chat's events after ``after``, oldest first.

    ``seq`` is the table's ``INTEGER PRIMARY KEY``, so it is monotonic across the
    whole profile rather than per chat.  That is what lets one connection carry
    every conversation under one cursor: a reader watching four chats at once
    holds one tail instead of four, which matters because each tail otherwise
    pins one of the server's forty threadpool slots for its whole life.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM chat_events WHERE seq > ? ORDER BY seq LIMIT ?",
            (after, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [_project(row) for row in rows]


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


def latest_seq_all() -> int:
    """The newest sequence number anywhere in the profile, or 0 when empty."""

    conn = _connect()
    try:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM chat_events").fetchone()
        return int(row["seq"]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def active_turns() -> dict[int, str]:
    """The status of each chat's unfinished plain generation.

    The generation half of what :func:`has_active_turn` asks per chat, shaped
    like ``agent_core.store.active_status_by_chat`` so the sidebar can merge the
    two without either side knowing about the other.  A chat with more than one
    unfinished row reports the oldest, which is the one a queue drains first.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT chat_id, status FROM chat_generations"
            " WHERE status IN ('queued', 'running') ORDER BY created_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(row["chat_id"]): str(row["status"]) for row in rows}


def any_active_turn() -> bool:
    """Whether any chat in the profile is still working.

    What closes the profile-wide tail.  It deliberately does not close when one
    chat reaches a terminal event, because the point of that tail is the chats
    that are still going.
    """

    conn = _connect()
    try:
        generation = conn.execute(
            "SELECT 1 FROM chat_generations WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
        if generation:
            return True
    except sqlite3.OperationalError:
        pass
    try:
        session = conn.execute(
            "SELECT 1 FROM workspace_agent_sessions"
            " WHERE status IN ('queued', 'running', 'waiting_approval') LIMIT 1"
        ).fetchone()
        return bool(session)
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


#: A runaway guard on how far a fresh reader is rewound.  Nothing should ever
#: hit it: it exists for the row that says ``running`` because a process was
#: killed between heartbeats and boot recovery has not yet failed it, which
#: would otherwise rewind every new connection to the dawn of the profile.
MAX_REPLAY_EVENTS = 2000


def first_seq_of_active_turns() -> int:
    """The earliest sequence number belonging to a turn still producing, else 0.

    Where a reader with no cursor of its own starts.  Rewinding to the beginning
    of everything still running is what rebuilds the narration of a chat that was
    generating while nobody was watching -- the profile-wide twin of what
    ``_stream_cursor`` does for one chat.

    ``waiting_approval`` is excluded, and that exclusion is load-bearing.  A run
    parked on a person is not narrating anything; it can sit for days, and a real
    profile here held one from nine days earlier whose first event was the sixth
    in the log.  Including it rewound every fresh connection over the entire
    history to replay a run that had produced nothing since.  A paused run is
    rebuilt when its chat is opened, from the session payload ``mergeLiveRun``
    already merges -- not from the live tail, whose job is what is happening now.
    The per-chat ``_stream_cursor`` still rewinds to it, because opening that
    chat is a request for exactly that run.
    """

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MIN(seq) AS seq FROM chat_events WHERE generation_id IN"
            " (SELECT id FROM chat_generations WHERE status IN ('queued', 'running'))"
        ).fetchone()
        generation_seq = int(row["seq"]) if row and row["seq"] is not None else 0
    except sqlite3.OperationalError:
        generation_seq = 0
    try:
        row = conn.execute(
            "SELECT MIN(seq) AS seq FROM chat_events WHERE agent_session_id IN"
            " (SELECT id FROM workspace_agent_sessions"
            "  WHERE status IN ('queued', 'running'))"
        ).fetchone()
        session_seq = int(row["seq"]) if row and row["seq"] is not None else 0
    except sqlite3.OperationalError:
        session_seq = 0
    finally:
        conn.close()
    candidates = [seq for seq in (generation_seq, session_seq) if seq]
    return min(candidates) if candidates else 0


def live_stream_start() -> int:
    """The ``after`` value a reader with no cursor of its own should begin at.

    The profile-wide generalisation of ``_stream_cursor``: rewind to just before
    the first event of everything still in flight, so a browser that reloads
    rebuilds the narration of every chat that was generating while nobody was
    watching, and replays nothing else.  A settled thread is fully described by
    its message rows, so starting at the end of the log is the right answer for
    one.
    """

    latest = latest_seq_all()
    first = first_seq_of_active_turns()
    if not first:
        return latest
    return max(first - 1, latest - MAX_REPLAY_EVENTS)


def _parse_heartbeat(raw: Any) -> datetime | None:
    """Read a heartbeat however its writer stored it.

    SQLAlchemy writes these as ``2026-08-02 12:15:42.971635`` -- space-separated
    and naive -- while this module's own timestamps are ISO with an offset.
    ``fromisoformat`` accepts both, which is why the comparison is done on
    datetimes here rather than on the strings in SQL, where the two formats do
    not order against each other at all.
    """

    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def running_turn_ids() -> tuple[dict[str, datetime | None], dict[str, datetime | None]]:
    """Running turns and their last heartbeat, as ``(generations, sessions)``.

    The heartbeat comes back with the id because whether a lease has gone stale
    is the caller's policy, not this module's -- and a row that says ``running``
    because its worker was killed must stop holding a slot, or three such rows
    would wedge a profile until someone happened to open each of those chats.

    ``waiting_approval`` is deliberately excluded: a run stopped on a person is
    not using the model, and holding a slot open for however long someone takes
    to read a diff would let three paused runs deadlock the whole profile.
    """

    conn = _connect()
    try:
        generations = {
            str(row["id"]): _parse_heartbeat(row["heartbeat_at"])
            for row in conn.execute(
                "SELECT id, heartbeat_at FROM chat_generations WHERE status = 'running'"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        generations = {}
    try:
        sessions = {
            str(row["id"]): _parse_heartbeat(row["heartbeat_at"])
            for row in conn.execute(
                "SELECT id, heartbeat_at FROM workspace_agent_sessions WHERE status = 'running'"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        sessions = {}
    finally:
        conn.close()
    return generations, sessions


def queued_generations(limit: int) -> list[tuple[str, int]]:
    """``(generation_id, chat_id)`` for waiting plain turns, oldest first.

    Oldest first because FIFO is the only order a user can predict, and the
    composer told them their turn was accepted the moment it was written.
    """

    if limit <= 0:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id FROM chat_generations WHERE status = 'queued'"
            " ORDER BY created_at, rowid LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(str(row["id"]), int(row["chat_id"])) for row in rows]


def queued_agent_sessions(limit: int) -> list[tuple[str, int]]:
    """``(session_id, chat_id)`` for waiting agent turns, oldest first."""

    if limit <= 0:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, chat_id FROM workspace_agent_sessions"
            " WHERE status = 'queued' AND chat_id IS NOT NULL"
            " ORDER BY created_at, rowid LIMIT ?",
            (int(limit),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [(str(row["id"]), int(row["chat_id"])) for row in rows]


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
    "active_turns",
    "any_active_turn",
    "append",
    "first_seq_for",
    "first_seq_of_active_turns",
    "has_active_turn",
    "latest_seq",
    "latest_seq_all",
    "list_all_events",
    "list_events",
    "live_stream_start",
    "queued_agent_sessions",
    "queued_generations",
    "running_turn_ids",
]
