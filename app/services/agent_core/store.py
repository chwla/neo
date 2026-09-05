"""SQLite persistence for agent sessions.

Follows the convention used across Neo's service stores: raw ``sqlite3``,
idempotent ``CREATE TABLE IF NOT EXISTS`` at startup, one connection per call,
and a column allowlist on updates.

The one structural departure is ``workspace_agent_events``: an append-only log
with a monotonic sequence number. It is what makes a run both streamable and
resumable from a single mechanism -- a reconnecting browser replays from the
last sequence it saw rather than re-reading the whole run.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

ACTIVE_STATUSES = {"queued", "running", "waiting_approval"}


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


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def get_sidebar_project(project_id: str | int) -> dict | None:
    """Read one sidebar project, the same ones a chat can be filed under.

    Agent runs are filed against the chat store's projects so that a run and a
    conversation about the same work land in the same place in the sidebar. That
    table belongs to the ORM layer, but reading one row directly keeps the agent
    loop free of a session dependency.
    """

    try:
        identifier = int(project_id)
    except (TypeError, ValueError):
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id, name FROM projects WHERE id = ?", (identifier,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    # The prompt builder reads `title`; the chat store calls the column `name`.
    return {"id": row["id"], "title": row["name"]} if row else None


def initialize_agent_core_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_agent_sessions (
                id TEXT PRIMARY KEY,
                objective TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                mode TEXT NOT NULL DEFAULT 'normal',
                stop_reason TEXT,
                project_id TEXT,
                repo_id TEXT,
                task_id TEXT,
                agent_definition_id TEXT,
                agent_definition_snapshot_json TEXT,
                disabled_tools_json TEXT,
                -- The chat this run is a turn of, and the assistant row that
                -- holds its place in that chat's transcript.
                chat_id INTEGER,
                anchor_message_id INTEGER,
                -- Which engine runs this session: Neo's own loop, or an external
                -- coding CLI driven as a subprocess.  Defaulted rather than
                -- nullable so every row predating external agents reads as 'neo'
                -- without a backfill pass.
                executor TEXT NOT NULL DEFAULT 'neo',
                -- The external CLI's own conversation id for the executor that
                -- ran most recently.  Claude Code takes one we assign; Codex
                -- mints its own and we record what it reports.
                external_session_id TEXT,
                -- Per-executor ids and usage, keyed by executor name, so an
                -- A -> B -> A sequence can resume each side independently.
                external_meta_json TEXT,
                -- The chain plan and which step is current, for a handoff.
                handoff_json TEXT,
                todo_json TEXT,
                evidence_json TEXT,
                budgets_json TEXT,
                iterations INTEGER NOT NULL DEFAULT 0,
                tool_call_count INTEGER NOT NULL DEFAULT 0,
                consecutive_errors INTEGER NOT NULL DEFAULT 0,
                adjudications INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                error TEXT,
                client_request_id TEXT,
                worker_id TEXT,
                lease_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                heartbeat_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_sessions_request
                ON workspace_agent_sessions(client_request_id)
                WHERE client_request_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_agent_sessions_status
                ON workspace_agent_sessions(status, updated_at DESC);
            CREATE INDEX IF NOT EXISTS ix_agent_sessions_task
                ON workspace_agent_sessions(task_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS workspace_agent_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tool_calls_json TEXT,
                tool_call_id TEXT,
                name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ix_agent_messages_seq
                ON workspace_agent_messages(session_id, seq);

            CREATE TABLE IF NOT EXISTS workspace_agent_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_agent_events_session
                ON workspace_agent_events(session_id, seq);

            CREATE TABLE IF NOT EXISTS workspace_agent_approvals (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_hash TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                grantable INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                decided_at TEXT,
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_agent_approvals_session
                ON workspace_agent_approvals(session_id, status);

            CREATE TABLE IF NOT EXISTS workspace_agent_grants (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                predicate_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_agent_grants_session
                ON workspace_agent_grants(session_id);

            CREATE TABLE IF NOT EXISTS workspace_agent_tool_calls (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT,
                status TEXT NOT NULL,
                content TEXT,
                error TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_agent_tool_calls_session
                ON workspace_agent_tool_calls(session_id, created_at);

            CREATE TABLE IF NOT EXISTS workspace_agent_file_snapshots (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                repo_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                existed_before INTEGER NOT NULL DEFAULT 1,
                before_text TEXT,
                -- Restoring LF into a file that was CRLF would leave the user a
                -- whole-file diff after an undo that was meant to leave nothing.
                before_newline TEXT NOT NULL DEFAULT '\n',
                -- What the run left behind, refreshed on every write. Undo
                -- compares the file against this to tell "the agent wrote this"
                -- from "someone has edited it since", and refuses the latter.
                after_sha256 TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(session_id, relative_path),
                FOREIGN KEY (session_id) REFERENCES workspace_agent_sessions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_agent_file_snapshots_session
                ON workspace_agent_file_snapshots(session_id, relative_path);
        """)
        # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so the
        # columns the unified thread added have to be applied separately -- and
        # before anything indexes them. An index on `chat_id` inside the script
        # above would run against a table that does not have the column yet, so
        # every start against an existing database would fail there.
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(workspace_agent_sessions)").fetchall()
        }
        for name in ("chat_id", "anchor_message_id"):
            if name not in existing:
                conn.execute(f"ALTER TABLE workspace_agent_sessions ADD COLUMN {name} INTEGER")
        if "disabled_tools_json" not in existing:
            conn.execute(
                "ALTER TABLE workspace_agent_sessions ADD COLUMN disabled_tools_json TEXT"
            )
        # The external-executor columns, added the same way and for the same
        # reason.  `executor` carries its default so existing rows -- every one
        # of which was Neo's own loop -- read correctly without an UPDATE.
        if "executor" not in existing:
            conn.execute(
                "ALTER TABLE workspace_agent_sessions"
                " ADD COLUMN executor TEXT NOT NULL DEFAULT 'neo'"
            )
        for name in ("external_session_id", "external_meta_json", "handoff_json"):
            if name not in existing:
                conn.execute(f"ALTER TABLE workspace_agent_sessions ADD COLUMN {name} TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_agent_sessions_chat"
            " ON workspace_agent_sessions(chat_id, created_at)"
        )
        conn.commit()
    finally:
        conn.close()


def delete_chatless_sessions() -> int:
    """Drop runs that belong to no chat, once.

    Every run is now a turn of a conversation.  Sessions from before that have
    no chat to appear in and no surface left that could open them, so they are
    removed rather than left as rows only the database knows about.  The child
    tables go with them through their existing `ON DELETE CASCADE`.

    Guarded by a marker so a later run started outside a chat -- which would be
    a bug, not history -- is not silently deleted at the next restart.
    """

    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workspace_agent_migrations ("
            " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        already = conn.execute(
            "SELECT 1 FROM workspace_agent_migrations WHERE name = ?",
            ("drop_chatless_sessions",),
        ).fetchone()
        if already:
            return 0
        cursor = conn.execute("DELETE FROM workspace_agent_sessions WHERE chat_id IS NULL")
        conn.execute(
            "INSERT INTO workspace_agent_migrations (name, applied_at) VALUES (?,?)",
            ("drop_chatless_sessions", now_iso()),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _row_to_session(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["agent_definition_snapshot"] = _loads(
        data.pop("agent_definition_snapshot_json", None), None
    )
    data["disabled_tools"] = _loads(data.pop("disabled_tools_json", None), [])
    data["todo"] = _loads(data.pop("todo_json", None), [])
    data["evidence"] = _loads(data.pop("evidence_json", None), [])
    data["budgets"] = _loads(data.pop("budgets_json", None), {}) or {}
    data["external_meta"] = _loads(data.pop("external_meta_json", None), {}) or {}
    data["handoff"] = _loads(data.pop("handoff_json", None), None)
    data.setdefault("executor", "neo")
    return data


def _row_to_message(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["tool_calls"] = _loads(data.pop("tool_calls_json", None), [])
    return data


def insert_session(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_sessions (
                id, objective, title, status, mode, project_id, repo_id, task_id,
                agent_definition_id, agent_definition_snapshot_json, disabled_tools_json,
                chat_id, anchor_message_id, executor, external_session_id,
                external_meta_json, handoff_json, todo_json, evidence_json, budgets_json,
                client_request_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item["id"],
                item["objective"],
                item["title"],
                item.get("status", "queued"),
                item.get("mode", "normal"),
                item.get("project_id"),
                item.get("repo_id"),
                item.get("task_id"),
                item.get("agent_definition_id"),
                json.dumps(item.get("agent_definition_snapshot"))
                if item.get("agent_definition_snapshot")
                else None,
                json.dumps(item.get("disabled_tools") or []),
                item.get("chat_id"),
                item.get("anchor_message_id"),
                item.get("executor") or "neo",
                item.get("external_session_id"),
                json.dumps(item.get("external_meta") or {}),
                json.dumps(item.get("handoff")) if item.get("handoff") else None,
                json.dumps(item.get("todo") or []),
                json.dumps(item.get("evidence") or []),
                json.dumps(item.get("budgets") or {}),
                item.get("client_request_id"),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(item["id"]) or item


def get_session(session_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def get_session_by_request(client_request_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_sessions WHERE client_request_id = ?",
            (client_request_id,),
        ).fetchone()
        return _row_to_session(row) if row else None
    finally:
        conn.close()


def list_sessions(*, limit: int = 25, task_id: str | None = None) -> list[dict]:
    conn = _connect()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM workspace_agent_sessions WHERE task_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM workspace_agent_sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_session(row) for row in rows]
    finally:
        conn.close()


_SESSION_COLUMNS = {
    "status",
    "mode",
    "stop_reason",
    "todo",
    "evidence",
    "budgets",
    "iterations",
    "tool_call_count",
    "consecutive_errors",
    "adjudications",
    "summary",
    "error",
    "worker_id",
    "lease_token",
    "started_at",
    "completed_at",
    "heartbeat_at",
    "agent_definition_snapshot",
    "chat_id",
    "anchor_message_id",
    "executor",
    "external_session_id",
    "external_meta",
    "handoff",
}
_JSON_COLUMNS = {
    "todo": "todo_json",
    "evidence": "evidence_json",
    "budgets": "budgets_json",
    "agent_definition_snapshot": "agent_definition_snapshot_json",
    "external_meta": "external_meta_json",
    "handoff": "handoff_json",
}


def update_session(session_id: str, updates: dict) -> dict | None:
    clean = {key: value for key, value in updates.items() if key in _SESSION_COLUMNS}
    if not clean:
        return get_session(session_id)
    for key, column in _JSON_COLUMNS.items():
        if key in clean:
            clean[column] = json.dumps(clean.pop(key))
    clean["updated_at"] = now_iso()
    columns = ", ".join(f"{name} = ?" for name in clean)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_agent_sessions SET {columns} WHERE id = ?",
            [*clean.values(), session_id],
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id)


def cancel_session(session_id: str) -> dict | None:
    """Cooperative cancel: flip the row and let the worker notice at its next check."""

    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_agent_sessions SET status = 'cancelled', stop_reason = 'cancelled',"
            " completed_at = ?, updated_at = ? WHERE id = ?"
            " AND status IN ('queued', 'running', 'waiting_approval')",
            (now_iso(), now_iso(), session_id),
        )
        conn.commit()
    finally:
        conn.close()
    return get_session(session_id)


# --- transcript ---------------------------------------------------------------


def append_message(session_id: str, message: dict) -> dict:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM workspace_agent_messages"
            " WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = int(row["next"])
        identifier = message.get("id") or new_id()
        conn.execute(
            """
            INSERT INTO workspace_agent_messages
                (id, session_id, seq, role, content, tool_calls_json, tool_call_id,
                 name, created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                identifier,
                session_id,
                seq,
                message["role"],
                message.get("content") or "",
                json.dumps(message.get("tool_calls") or []) if message.get("tool_calls") else None,
                message.get("tool_call_id"),
                message.get("name"),
                message.get("created_at") or now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": identifier, "seq": seq, **message}


def list_messages(session_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        return [_row_to_message(row) for row in rows]
    finally:
        conn.close()


# --- events -------------------------------------------------------------------


def append_event(session_id: str, event_type: str, payload: dict | None = None) -> int:
    """Append to the run's own log, and mirror into its chat's log.

    The session log stays the run's record -- it backs
    ``GET /agent-sessions/{id}/events``, the CLI, and resumption -- while the
    chat log is what a thread mixing plain replies and agent turns is read from.
    Two sequences rather than one because each reader needs a cursor that only
    its own stream advances.
    """

    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO workspace_agent_events (session_id, event_type, payload_json, created_at)"
            " VALUES (?,?,?,?)",
            (session_id, event_type, json.dumps(payload or {}, default=str), now_iso()),
        )
        conn.commit()
        seq = int(cursor.lastrowid or 0)
        row = conn.execute(
            "SELECT chat_id, anchor_message_id FROM workspace_agent_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row and row["chat_id"] is not None:
        from app.services import chat_events

        chat_events.append(
            int(row["chat_id"]),
            event_type,
            payload,
            message_id=row["anchor_message_id"],
            agent_session_id=session_id,
        )
    return seq


def active_status_by_chat() -> dict[int, str]:
    """The status of each chat's unfinished run, for the sidebar badge.

    Only unfinished runs are reported: a chat whose last agent turn is done is
    an ordinary chat again, and badging it would make every thread that ever
    used the agent look permanently special.
    """

    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = conn.execute(
            "SELECT chat_id, status FROM workspace_agent_sessions"
            f" WHERE chat_id IS NOT NULL AND status IN ({placeholders})"
            " ORDER BY created_at",
            tuple(ACTIVE_STATUSES),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(row["chat_id"]): str(row["status"]) for row in rows}


def sessions_for_chat(chat_id: int) -> list[dict]:
    """Every run that is a turn of this chat, oldest first."""

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_sessions WHERE chat_id = ? ORDER BY created_at",
            (int(chat_id),),
        ).fetchall()
        return [_row_to_session(row) for row in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def create_chat_for_session(objective: str, title: str) -> tuple[int, int]:
    """Open a conversation for a run that was not started from one.

    Every run is a turn of a chat -- that is the whole shape of the thing now --
    but a run can still be started from a task, or from the CLI, where no chat
    exists yet. Rather than let those become sessions nothing can open, they get
    a conversation of their own, indistinguishable afterwards from one begun by
    typing into the composer.

    Written directly for the same reason `update_anchor_message` is: this runs
    below the request layer, with no ORM session to hand.

    Returns the new chat's id and the assistant row holding the run's place.
    """

    now = now_iso()
    conn = _connect()
    try:
        chat = conn.execute(
            "INSERT INTO chats (title, project_id, archived, pinned, agent_mode,"
            " disabled_tools, created_at, updated_at) VALUES (?,?,0,0,?,?,?,?)",
            (title[:160], None, "normal", "[]", now, now),
        )
        chat_id = int(chat.lastrowid or 0)
        conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, created_at) VALUES (?,?,?,?)",
            (chat_id, "user", objective, now),
        )
        anchor = conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, response_kind, created_at)"
            " VALUES (?,?,?,?,?)",
            (chat_id, "assistant", "", "agent_run", now),
        )
        conn.commit()
        return chat_id, int(anchor.lastrowid or 0)
    finally:
        conn.close()


def set_anchor_session(message_id: int, session_id: str) -> None:
    """Point an anchor row at the run that fills it in."""

    conn = _connect()
    try:
        conn.execute(
            "UPDATE chat_messages SET metadata_json = ? WHERE id = ?",
            (json.dumps({"agent_session_id": session_id}, sort_keys=True), int(message_id)),
        )
        conn.commit()
    finally:
        conn.close()


def executors_for(session_ids) -> dict[str, str]:
    """Which engine each of these sessions runs on.

    Used by the concurrency cap, which has to tell an external turn from a local
    one to apply the external sub-cap. Reads in one query rather than per id
    because it is called on the admission path, under a lock.
    """

    ids = [str(identifier) for identifier in session_ids if identifier]
    if not ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT id, executor FROM workspace_agent_sessions WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {row["id"]: (row["executor"] or "neo") for row in rows}


#: Keys an external CLI's conversation handle may be recorded under. Engine
#: names deliberately absent: this module stays ignorant of which CLI is which.
_EXTERNAL_ID_KEYS = ("session_id", "thread_id")


def last_external_session_id(
    chat_id: int, executor: str, *, exclude: str | None = None
) -> str | None:
    """The conversation id this executor last used in this chat.

    Continuity across turns: two consecutive Claude Code turns should continue
    one Claude conversation, not open a second. Scoped per executor and read from
    the most recent session that actually recorded one, so an intervening turn by
    a *different* executor -- or a failed run that never got an id -- does not
    break the chain.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, external_meta_json, external_session_id"
            " FROM workspace_agent_sessions"
            " WHERE chat_id = ? AND executor = ? AND external_session_id IS NOT NULL"
            " ORDER BY created_at DESC, rowid DESC LIMIT 20",
            (chat_id, executor),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()

    for row in rows:
        if exclude and row["id"] == exclude:
            continue
        meta = _loads(row["external_meta_json"], {}) or {}
        # CLIs name their conversation handle differently -- one calls it a
        # session, another a thread. The shared store has no business knowing
        # which is which, so it accepts either and lets the external layer own
        # the naming (``ExecutorSpec.session_id_key``).
        recorded = next(
            (
                (meta.get(executor) or {}).get(key)
                for key in _EXTERNAL_ID_KEYS
                if (meta.get(executor) or {}).get(key)
            ),
            None,
        )
        if recorded:
            return str(recorded)
        if row["external_session_id"]:
            return str(row["external_session_id"])
    return None


def chat_history_before(chat_id: int, message_id: int, limit: int = 40) -> list[dict]:
    """The conversation a run is joining, as role/content rows.

    An agent turn in a chat is a turn of that chat, so it starts from what was
    already said rather than from its objective alone -- otherwise "now do that
    for the other module" means nothing.  Rows are read directly for the same
    reason `update_anchor_message` writes them directly: the loop has no
    request-scoped session.

    Only rows before the anchor are returned, so a run never reads its own
    output, and empty rows are dropped -- an agent turn still in flight has an
    anchor with no content yet, and a blank assistant turn teaches the model
    that empty replies are acceptable.
    """

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_messages"
            " WHERE chat_id = ? AND id < ? AND role IN ('user', 'assistant')"
            " AND content IS NOT NULL AND TRIM(content) != ''"
            " ORDER BY id DESC LIMIT ?",
            (int(chat_id), int(message_id), max(0, limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def update_anchor_message(message_id: int, fields: dict) -> None:
    """Write a finished run's answer back onto its row in the chat transcript.

    The anchor is a `chat_messages` row owned by the ORM layer, but the loop
    that finishes a run has no request-scoped session.  Writing it directly
    follows the precedent set by `get_sidebar_project` above: the same database
    file, one narrow statement, and no session dependency in the agent loop.
    """

    allowed = {
        "content",
        "thinking",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "duration_ms",
        "provider_name",
        "model_name",
        "route_name",
        "finish_reason",
    }
    clean = {key: value for key, value in fields.items() if key in allowed}
    if not clean or not message_id:
        return
    columns = ", ".join(f"{name} = ?" for name in clean)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE chat_messages SET {columns} WHERE id = ?",
            [*clean.values(), int(message_id)],
        )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def list_events(session_id: str, after: int = 0, limit: int = 500) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_events WHERE session_id = ? AND seq > ?"
            " ORDER BY seq LIMIT ?",
            (session_id, after, limit),
        ).fetchall()
        return [
            {
                "seq": row["seq"],
                "type": row["event_type"],
                "created_at": row["created_at"],
                **_loads(row["payload_json"], {}),
            }
            for row in rows
        ]
    finally:
        conn.close()


# --- approvals ----------------------------------------------------------------


def insert_approval(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_approvals
                (id, session_id, call_id, tool_name, arguments_json, arguments_hash,
                 reason, grantable, status, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item["id"],
                item["session_id"],
                item["call_id"],
                item["tool_name"],
                json.dumps(item.get("arguments") or {}),
                item["arguments_hash"],
                item.get("reason", ""),
                1 if item.get("grantable") else 0,
                item.get("status", "pending"),
                item["created_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_approval(item["id"]) or item


def _row_to_approval(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["arguments"] = _loads(data.pop("arguments_json", None), {})
    data["grantable"] = bool(data.get("grantable"))
    return data


def get_approval(approval_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return _row_to_approval(row) if row else None
    finally:
        conn.close()


def pending_approval(session_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_approvals WHERE session_id = ? AND status = 'pending'"
            " ORDER BY created_at LIMIT 1",
            (session_id,),
        ).fetchone()
        return _row_to_approval(row) if row else None
    finally:
        conn.close()


def decide_approval(approval_id: str, approved: bool) -> dict | None:
    """Move a pending approval to a decision, once.

    The ``status = 'pending'`` predicate in the UPDATE is what makes the
    transition atomic: two concurrent approvals race on the same row and exactly
    one of them changes it.
    """

    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE workspace_agent_approvals SET status = ?, decided_at = ?"
            " WHERE id = ? AND status = 'pending'",
            ("approved" if approved else "rejected", now_iso(), approval_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_approval(approval_id)


def claim_approval(approval_id: str, arguments_hash: str) -> dict | None:
    """Consume an approved call exactly once, and only for the frozen arguments.

    This is the single-use gate. ``status = 'approved'`` in the WHERE clause means
    a second execution finds nothing to claim, and the hash predicate means an
    approval cannot be redirected onto different arguments than the user saw.
    """

    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE workspace_agent_approvals SET status = 'consumed', decided_at = ?"
            " WHERE id = ? AND status = 'approved' AND arguments_hash = ?",
            (now_iso(), approval_id, arguments_hash),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_approval(approval_id)


# --- grants -------------------------------------------------------------------


def insert_grant(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_agent_grants"
            " (id, session_id, tool_name, predicate_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                item["id"],
                item["session_id"],
                item["tool_name"],
                json.dumps(item.get("predicate") or {}),
                item["created_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return item


def list_grants(session_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_grants WHERE session_id = ?", (session_id,)
        ).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "tool_name": row["tool_name"],
                "predicate": _loads(row["predicate_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        conn.close()


# --- tool call audit ----------------------------------------------------------


def record_tool_call(session_id: str, result: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_tool_calls
                (id, session_id, call_id, tool_name, arguments_json, status, content,
                 error, duration_ms, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                new_id(),
                session_id,
                result["call_id"],
                result["name"],
                json.dumps(result.get("arguments") or {}),
                result.get("status", "ok"),
                (result.get("content") or "")[:20000],
                result.get("error"),
                int(result.get("duration_ms") or 0),
                now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_tool_calls(session_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_tool_calls WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [{**dict(row), "arguments": _loads(row["arguments_json"], {})} for row in rows]
    finally:
        conn.close()


# --- worker leases ------------------------------------------------------------

#: A worker must refresh its lease within this window or be considered dead. It
#: is generous because a single tool call (a test run) can legitimately take
#: minutes without the loop getting a chance to heartbeat.
LEASE_SECONDS = 300


def _stale_before() -> str:
    from datetime import timedelta

    return (datetime.now(UTC) - timedelta(seconds=LEASE_SECONDS)).isoformat()


def claim_session(session_id: str, worker_id: str, lease_token: str) -> dict | None:
    """Take ownership of a session, or return None if someone else holds it.

    The predicate is the whole mechanism: a session can be claimed only when it
    is startable and either unowned or held by a worker that has stopped
    heartbeating. Two workers racing on the same session means exactly one
    UPDATE matches.
    """

    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE workspace_agent_sessions"
            " SET worker_id = ?, lease_token = ?, heartbeat_at = ?, updated_at = ?"
            " WHERE id = ?"
            # 'waiting_approval' is claimable because resuming an approved call is
            # exactly what has to happen next; without it an approved session
            # would sit suspended forever with no worker willing to take it.
            "   AND status IN ('queued', 'running', 'waiting_approval')"
            "   AND (worker_id IS NULL OR heartbeat_at IS NULL OR heartbeat_at < ?)",
            (worker_id, lease_token, now_iso(), now_iso(), session_id, _stale_before()),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return None
    finally:
        conn.close()
    return get_session(session_id)


def heartbeat(session_id: str, lease_token: str) -> bool:
    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE workspace_agent_sessions SET heartbeat_at = ? WHERE id = ? AND lease_token = ?",
            (now_iso(), session_id, lease_token),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def release_session(session_id: str, lease_token: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_agent_sessions SET worker_id = NULL, lease_token = NULL"
            " WHERE id = ? AND lease_token = ?",
            (session_id, lease_token),
        )
        conn.commit()
    finally:
        conn.close()


def recover_interrupted_sessions() -> int:
    """Fail sessions abandoned by a dead worker, at startup.

    A process restart leaves rows in 'running' that nothing will ever advance.
    Marking them failed is honest; leaving them active would show the user a
    spinner for a run that no longer exists.
    """

    conn = _connect()
    try:
        cursor = conn.execute(
            "UPDATE workspace_agent_sessions"
            " SET status = 'failed', stop_reason = 'failed',"
            "     error = 'Neo restarted while this run was in progress.',"
            "     completed_at = ?, updated_at = ?, worker_id = NULL, lease_token = NULL"
            " WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (now_iso(), now_iso(), _stale_before()),
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def insert_file_snapshot(item: dict) -> None:
    """Record a file's pre-edit state, keeping only the first touch per session.

    ``INSERT OR IGNORE`` against the ``(session_id, relative_path)`` unique index
    is what makes "before" mean before the run: a second write to the same file
    finds a row already there and leaves it alone.
    """

    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO workspace_agent_file_snapshots"
            " (id, session_id, repo_id, relative_path, existed_before, before_text,"
            "  before_newline, after_sha256, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                item["id"],
                item["session_id"],
                item["repo_id"],
                item["relative_path"],
                int(bool(item.get("existed_before", True))),
                item.get("before_text"),
                item.get("before_newline") or "\n",
                item.get("after_sha256"),
                item["created_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def record_snapshot_result(session_id: str, relative_path: str, after_sha256: str) -> None:
    """Refresh what the run left behind, so undo can detect later outside edits."""

    conn = _connect()
    try:
        conn.execute(
            "UPDATE workspace_agent_file_snapshots SET after_sha256 = ?"
            " WHERE session_id = ? AND relative_path = ?",
            (after_sha256, session_id, relative_path),
        )
        conn.commit()
    finally:
        conn.close()


def list_file_snapshots(session_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_file_snapshots WHERE session_id = ?"
            " ORDER BY relative_path",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def clear_file_snapshots(session_id: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "DELETE FROM workspace_agent_file_snapshots WHERE session_id = ?", (session_id,)
        )
        conn.commit()
    finally:
        conn.close()
