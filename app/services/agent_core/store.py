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
        conn.commit()
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
    data["todo"] = _loads(data.pop("todo_json", None), [])
    data["evidence"] = _loads(data.pop("evidence_json", None), [])
    data["budgets"] = _loads(data.pop("budgets_json", None), {}) or {}
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
                agent_definition_id, agent_definition_snapshot_json, todo_json,
                evidence_json, budgets_json, client_request_id, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
}
_JSON_COLUMNS = {
    "todo": "todo_json",
    "evidence": "evidence_json",
    "budgets": "budgets_json",
    "agent_definition_snapshot": "agent_definition_snapshot_json",
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
    conn = _connect()
    try:
        cursor = conn.execute(
            "INSERT INTO workspace_agent_events (session_id, event_type, payload_json, created_at)"
            " VALUES (?,?,?,?)",
            (session_id, event_type, json.dumps(payload or {}), now_iso()),
        )
        conn.commit()
        return int(cursor.lastrowid or 0)
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
