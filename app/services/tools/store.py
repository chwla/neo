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


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_tool_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_tool_servers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                server_type TEXT NOT NULL,
                command_json TEXT,
                url TEXT,
                env_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                approval_required INTEGER NOT NULL DEFAULT 1,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_tool_definitions (
                id TEXT PRIMARY KEY,
                server_id TEXT,
                name TEXT NOT NULL,
                display_name TEXT,
                description TEXT,
                category TEXT NOT NULL,
                input_schema_json TEXT,
                output_schema_json TEXT,
                permissions_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                built_in INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (server_id) REFERENCES workspace_tool_servers(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_skill_definitions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT,
                description TEXT,
                skill_type TEXT NOT NULL,
                instructions TEXT NOT NULL,
                tool_ids_json TEXT,
                agent_ids_json TEXT,
                rules_profile_ids_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                built_in INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_tool_calls (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                coding_run_id TEXT,
                agent_definition_id TEXT,
                tool_id TEXT NOT NULL,
                skill_id TEXT,
                status TEXT NOT NULL,
                approval_status TEXT NOT NULL DEFAULT 'not_required',
                input_json TEXT,
                output_json TEXT,
                error TEXT,
                latency_ms INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (tool_id) REFERENCES workspace_tool_definitions(id),
                FOREIGN KEY (skill_id) REFERENCES workspace_skill_definitions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_tool_servers_type
            ON workspace_tool_servers(server_type, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_tool_definitions_server
            ON workspace_tool_definitions(server_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_tool_definitions_name
            ON workspace_tool_definitions(name, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_skill_definitions_name
            ON workspace_skill_definitions(name, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_tool_calls_status
            ON workspace_tool_calls(status, approval_status, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_tool_calls_run
            ON workspace_tool_calls(run_id, coding_run_id, created_at);
        """)
        _ensure_column(conn, "workspace_agent_definitions", "skill_ids_json", "TEXT")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, spec: str) -> None:
    try:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return
    if not columns:
        return
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def upsert_server(item: dict) -> dict:
    values = _server_values(item)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tool_servers (
                id, name, server_type, command_json, url, env_json, enabled,
                approval_required, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :server_type, :command_json, :url, :env_json, :enabled,
                :approval_required, :metadata_json, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, server_type=excluded.server_type,
                command_json=excluded.command_json, url=excluded.url, env_json=excluded.env_json,
                enabled=excluded.enabled, approval_required=excluded.approval_required,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """,
            values,
        )
        conn.commit()
        return get_server(item["id"]) or item
    finally:
        conn.close()


def insert_server(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tool_servers (
                id, name, server_type, command_json, url, env_json, enabled,
                approval_required, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :server_type, :command_json, :url, :env_json, :enabled,
                :approval_required, :metadata_json, :created_at, :updated_at
            )
            """,
            _server_values(item),
        )
        conn.commit()
        return get_server(item["id"]) or item
    finally:
        conn.close()


def get_server(server_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_tool_servers WHERE id=?", (server_id,)
        ).fetchone()
        return _server(row) if row else None
    finally:
        conn.close()


def list_servers(*, include_disabled: bool = True) -> list[dict]:
    where = "1=1" if include_disabled else "enabled=1"
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_tool_servers WHERE {where} ORDER BY name ASC"
        ).fetchall()
        return [_server(row) for row in rows]
    finally:
        conn.close()


def update_server(server_id: str, updates: dict) -> dict | None:
    return _update("workspace_tool_servers", server_id, updates, _SERVER_JSON, get_server)


def upsert_tool(item: dict) -> dict:
    values = _tool_values(item)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tool_definitions (
                id, server_id, name, display_name, description, category,
                input_schema_json, output_schema_json, permissions_json, enabled,
                built_in, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :server_id, :name, :display_name, :description, :category,
                :input_schema_json, :output_schema_json, :permissions_json, :enabled,
                :built_in, :metadata_json, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                server_id=excluded.server_id, name=excluded.name,
                display_name=excluded.display_name,
                description=excluded.description, category=excluded.category,
                input_schema_json=excluded.input_schema_json,
                output_schema_json=excluded.output_schema_json,
                permissions_json=excluded.permissions_json, enabled=excluded.enabled,
                built_in=excluded.built_in, metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            values,
        )
        conn.commit()
        return get_tool(item["id"]) or item
    finally:
        conn.close()


def insert_tool(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tool_definitions (
                id, server_id, name, display_name, description, category,
                input_schema_json, output_schema_json, permissions_json, enabled,
                built_in, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :server_id, :name, :display_name, :description, :category,
                :input_schema_json, :output_schema_json, :permissions_json, :enabled,
                :built_in, :metadata_json, :created_at, :updated_at
            )
            """,
            _tool_values(item),
        )
        conn.commit()
        return get_tool(item["id"]) or item
    finally:
        conn.close()


def get_tool(tool_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_tool_definitions WHERE id=? OR name=?",
            (tool_id, tool_id),
        ).fetchone()
        return _tool(row) if row else None
    finally:
        conn.close()


def list_tools(*, include_disabled: bool = True, server_id: str | None = None) -> list[dict]:
    where, params = ["1=1"], []
    if not include_disabled:
        where.append("enabled=1")
    if server_id:
        where.append("server_id=?")
        params.append(server_id)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_tool_definitions WHERE "
            + " AND ".join(where)
            + " ORDER BY built_in DESC, name ASC",
            params,
        ).fetchall()
        return [_tool(row) for row in rows]
    finally:
        conn.close()


def update_tool(tool_id: str, updates: dict) -> dict | None:
    return _update("workspace_tool_definitions", tool_id, updates, _TOOL_JSON, get_tool)


def upsert_skill(item: dict) -> dict:
    values = _skill_values(item)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_skill_definitions (
                id, name, display_name, description, skill_type, instructions,
                tool_ids_json, agent_ids_json, rules_profile_ids_json, enabled,
                built_in, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :display_name, :description, :skill_type, :instructions,
                :tool_ids_json, :agent_ids_json, :rules_profile_ids_json, :enabled,
                :built_in, :metadata_json, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name, description=excluded.description,
                skill_type=excluded.skill_type, instructions=excluded.instructions,
                tool_ids_json=excluded.tool_ids_json, agent_ids_json=excluded.agent_ids_json,
                rules_profile_ids_json=excluded.rules_profile_ids_json, enabled=excluded.enabled,
                built_in=excluded.built_in, metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            values,
        )
        conn.commit()
        return get_skill(item["id"]) or item
    finally:
        conn.close()


def insert_skill(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_skill_definitions (
                id, name, display_name, description, skill_type, instructions,
                tool_ids_json, agent_ids_json, rules_profile_ids_json, enabled,
                built_in, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :display_name, :description, :skill_type, :instructions,
                :tool_ids_json, :agent_ids_json, :rules_profile_ids_json, :enabled,
                :built_in, :metadata_json, :created_at, :updated_at
            )
            """,
            _skill_values(item),
        )
        conn.commit()
        return get_skill(item["id"]) or item
    finally:
        conn.close()


def get_skill(skill_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_skill_definitions WHERE id=? OR name=?",
            (skill_id, skill_id),
        ).fetchone()
        return _skill(row) if row else None
    finally:
        conn.close()


def list_skills(*, include_disabled: bool = True) -> list[dict]:
    where = "1=1" if include_disabled else "enabled=1"
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_skill_definitions WHERE {where} "
            "ORDER BY built_in DESC, name ASC"
        ).fetchall()
        return [_skill(row) for row in rows]
    finally:
        conn.close()


def update_skill(skill_id: str, updates: dict) -> dict | None:
    return _update("workspace_skill_definitions", skill_id, updates, _SKILL_JSON, get_skill)


def insert_call(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_tool_calls (
                id, run_id, coding_run_id, agent_definition_id, tool_id, skill_id,
                status, approval_status, input_json, output_json, error, latency_ms,
                created_at, completed_at
            ) VALUES (
                :id, :run_id, :coding_run_id, :agent_definition_id, :tool_id, :skill_id,
                :status, :approval_status, :input_json, :output_json, :error, :latency_ms,
                :created_at, :completed_at
            )
            """,
            _call_values(item),
        )
        conn.commit()
        return get_call(item["id"]) or item
    finally:
        conn.close()


def get_call(call_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM workspace_tool_calls WHERE id=?", (call_id,)).fetchone()
        return _call(row) if row else None
    finally:
        conn.close()


def list_calls(
    *,
    run_id: str | None = None,
    coding_run_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where, params = ["1=1"], []
    for key, value in (
        ("run_id", run_id),
        ("coding_run_id", coding_run_id),
        ("status", status),
    ):
        if value:
            where.append(f"{key}=?")
            params.append(value)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM workspace_tool_calls WHERE {clause}", params)
            .fetchone()[0]
        )
        rows = conn.execute(
            f"SELECT * FROM workspace_tool_calls WHERE {clause} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, max(1, min(limit, 500)), max(0, offset)],
        ).fetchall()
        return [_call(row) for row in rows], total
    finally:
        conn.close()


def update_call(call_id: str, updates: dict) -> dict | None:
    return _update("workspace_tool_calls", call_id, updates, _CALL_JSON, get_call)


_SERVER_JSON = {
    "command_json": "command_json",
    "env_json": "env_json",
    "metadata": "metadata_json",
}
_TOOL_JSON = {
    "input_schema": "input_schema_json",
    "output_schema": "output_schema_json",
    "permissions": "permissions_json",
    "metadata": "metadata_json",
}
_SKILL_JSON = {
    "tool_ids": "tool_ids_json",
    "agent_ids": "agent_ids_json",
    "rules_profile_ids": "rules_profile_ids_json",
    "metadata": "metadata_json",
}
_CALL_JSON = {"input": "input_json", "output": "output_json"}


def _update(
    table: str, item_id: str, updates: dict, json_fields: dict[str, str], getter
) -> dict | None:
    clean = {key: value for key, value in updates.items() if value is not None}
    if not clean:
        return getter(item_id)
    clean["updated_at"] = now_iso() if table != "workspace_tool_calls" else clean.get("updated_at")
    columns, params = [], []
    for key, value in clean.items():
        if key == "updated_at" and value is None:
            continue
        column = json_fields.get(key, key)
        columns.append(f"{column}=?")
        if key in json_fields:
            params.append(json.dumps(value))
        elif key in {"enabled", "approval_required", "built_in"}:
            params.append(int(bool(value)))
        else:
            params.append(value)
    params.append(item_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE {table} SET {', '.join(columns)} WHERE id=?", params)
        conn.commit()
        return getter(item_id)
    finally:
        conn.close()


def _loads(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _server_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "command_json": json.dumps(item.get("command_json")) if item.get("command_json") else None,
        "env_json": json.dumps(item.get("env_json", {})),
        "metadata_json": json.dumps(item.get("metadata", {})),
        "enabled": int(bool(item.get("enabled", True))),
        "approval_required": int(bool(item.get("approval_required", True))),
        "created_at": item.get("created_at") or now,
        "updated_at": item.get("updated_at") or now,
    }


def _server(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["command_json"] = _loads(item.get("command_json"), None)
    item["env_json"] = _loads(item.pop("env_json", None), {})
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    item["enabled"] = bool(item["enabled"])
    item["approval_required"] = bool(item["approval_required"])
    return item


def _tool_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "input_schema_json": json.dumps(item.get("input_schema", {})),
        "output_schema_json": json.dumps(item.get("output_schema", {})),
        "permissions_json": json.dumps(item.get("permissions", {})),
        "metadata_json": json.dumps(item.get("metadata", {})),
        "enabled": int(bool(item.get("enabled", True))),
        "built_in": int(bool(item.get("built_in", False))),
        "created_at": item.get("created_at") or now,
        "updated_at": item.get("updated_at") or now,
    }


def _tool(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["input_schema"] = _loads(item.pop("input_schema_json", None), {})
    item["output_schema"] = _loads(item.pop("output_schema_json", None), {})
    item["permissions"] = _loads(item.pop("permissions_json", None), {})
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    item["enabled"] = bool(item["enabled"])
    item["built_in"] = bool(item["built_in"])
    return item


def _skill_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "tool_ids_json": json.dumps(item.get("tool_ids", [])),
        "agent_ids_json": json.dumps(item.get("agent_ids", [])),
        "rules_profile_ids_json": json.dumps(item.get("rules_profile_ids", [])),
        "metadata_json": json.dumps(item.get("metadata", {})),
        "enabled": int(bool(item.get("enabled", True))),
        "built_in": int(bool(item.get("built_in", False))),
        "created_at": item.get("created_at") or now,
        "updated_at": item.get("updated_at") or now,
    }


def _skill(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["tool_ids"] = _loads(item.pop("tool_ids_json", None), [])
    item["agent_ids"] = _loads(item.pop("agent_ids_json", None), [])
    item["rules_profile_ids"] = _loads(item.pop("rules_profile_ids_json", None), [])
    item["metadata"] = _loads(item.pop("metadata_json", None), {})
    item["enabled"] = bool(item["enabled"])
    item["built_in"] = bool(item["built_in"])
    return item


def _call_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "input_json": json.dumps(item.get("input", {})),
        "output_json": json.dumps(item.get("output")) if item.get("output") is not None else None,
        "created_at": item.get("created_at") or now,
    }


def _call(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["input"] = _loads(item.pop("input_json", None), {})
    item["output"] = _loads(item.pop("output_json", None), None)
    return item
