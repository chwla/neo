from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings

JSON_FIELDS = {
    "rules_profile_ids": "rules_profile_ids_json",
    "permissions": "permissions_json",
    "tools": "tools_json",
    "metadata": "metadata_json",
}
DELEGATION_JSON_FIELDS = {"input": "input_json", "output": "output_json"}


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


def initialize_agent_framework_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_agent_definitions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT,
                description TEXT,
                agent_type TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                default_route_name TEXT,
                rules_profile_ids_json TEXT,
                permissions_json TEXT NOT NULL,
                tools_json TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                built_in INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 100,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_agent_delegations (
                id TEXT PRIMARY KEY,
                parent_run_id TEXT NOT NULL,
                child_run_id TEXT,
                parent_agent_id TEXT,
                child_agent_id TEXT NOT NULL,
                delegation_type TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT,
                output_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_definitions_type
            ON workspace_agent_definitions(agent_type, enabled, priority);
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_delegations_parent
            ON workspace_agent_delegations(parent_run_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_agent_delegations_child
            ON workspace_agent_delegations(child_run_id);
        """)
        conn.commit()
    finally:
        conn.close()


def upsert_definition(item: dict) -> dict:
    values = _definition_values(item)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_definitions (
                id, name, display_name, description, agent_type, system_prompt,
                default_route_name, rules_profile_ids_json, permissions_json, tools_json,
                enabled, built_in, priority, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :display_name, :description, :agent_type, :system_prompt,
                :default_route_name, :rules_profile_ids_json, :permissions_json, :tools_json,
                :enabled, :built_in, :priority, :metadata_json, :created_at, :updated_at
            )
            ON CONFLICT(id) DO UPDATE SET
                display_name=excluded.display_name,
                description=excluded.description,
                agent_type=excluded.agent_type,
                system_prompt=excluded.system_prompt,
                default_route_name=excluded.default_route_name,
                rules_profile_ids_json=excluded.rules_profile_ids_json,
                permissions_json=excluded.permissions_json,
                tools_json=excluded.tools_json,
                enabled=excluded.enabled,
                built_in=excluded.built_in,
                priority=excluded.priority,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            values,
        )
        conn.commit()
        return get_definition(item["id"]) or item
    finally:
        conn.close()


def insert_definition(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_definitions (
                id, name, display_name, description, agent_type, system_prompt,
                default_route_name, rules_profile_ids_json, permissions_json, tools_json,
                enabled, built_in, priority, metadata_json, created_at, updated_at
            ) VALUES (
                :id, :name, :display_name, :description, :agent_type, :system_prompt,
                :default_route_name, :rules_profile_ids_json, :permissions_json, :tools_json,
                :enabled, :built_in, :priority, :metadata_json, :created_at, :updated_at
            )
            """,
            _definition_values(item),
        )
        conn.commit()
        return get_definition(item["id"]) or item
    finally:
        conn.close()


def get_definition(agent_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_definitions WHERE id=? OR name=?",
            (agent_id, agent_id),
        ).fetchone()
        return _definition(row) if row else None
    finally:
        conn.close()


def list_definitions(*, include_disabled: bool = True) -> list[dict]:
    where = "1=1" if include_disabled else "enabled=1"
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_agent_definitions WHERE {where} "
            "ORDER BY built_in DESC, priority ASC, name ASC"
        ).fetchall()
        return [_definition(row) for row in rows]
    finally:
        conn.close()


def update_definition(agent_id: str, updates: dict) -> dict | None:
    allowed = {
        "display_name",
        "description",
        "system_prompt",
        "default_route_name",
        "rules_profile_ids",
        "permissions",
        "tools",
        "enabled",
        "priority",
        "metadata",
    }
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        return get_definition(agent_id)
    clean["updated_at"] = now_iso()
    columns, params = [], []
    for key, value in clean.items():
        column = JSON_FIELDS.get(key, key)
        columns.append(f"{column}=?")
        if key in JSON_FIELDS:
            params.append(json.dumps(value))
        elif key == "enabled":
            params.append(int(bool(value)))
        else:
            params.append(value)
    params.append(agent_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_agent_definitions SET {', '.join(columns)} WHERE id=?",
            params,
        )
        conn.commit()
        return get_definition(agent_id)
    finally:
        conn.close()


def insert_delegation(item: dict) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO workspace_agent_delegations (
                id, parent_run_id, child_run_id, parent_agent_id, child_agent_id,
                delegation_type, objective, status, input_json, output_json, error,
                created_at, updated_at, completed_at
            ) VALUES (
                :id, :parent_run_id, :child_run_id, :parent_agent_id, :child_agent_id,
                :delegation_type, :objective, :status, :input_json, :output_json, :error,
                :created_at, :updated_at, :completed_at
            )
            """,
            _delegation_values(item),
        )
        conn.commit()
        return get_delegation(item["id"]) or item
    finally:
        conn.close()


def get_delegation(delegation_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM workspace_agent_delegations WHERE id=?", (delegation_id,)
        ).fetchone()
        return _delegation(row) if row else None
    finally:
        conn.close()


def list_delegations(
    *,
    parent_run_id: str | None = None,
    child_run_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict]:
    where, params = ["1=1"], []
    for key, value in (
        ("parent_run_id", parent_run_id),
        ("child_run_id", child_run_id),
        ("status", status),
    ):
        if value:
            where.append(f"{key}=?")
            params.append(value)
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_agent_delegations WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC LIMIT ?",
            [*params, max(1, min(limit, 500))],
        ).fetchall()
        return [_delegation(row) for row in rows]
    finally:
        conn.close()


def update_delegation(delegation_id: str, updates: dict) -> dict | None:
    allowed = {"status", "child_run_id", "output", "error", "completed_at"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        return get_delegation(delegation_id)
    clean["updated_at"] = now_iso()
    columns, params = [], []
    for key, value in clean.items():
        column = DELEGATION_JSON_FIELDS.get(key, key)
        columns.append(f"{column}=?")
        params.append(json.dumps(value) if key in DELEGATION_JSON_FIELDS else value)
    params.append(delegation_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_agent_delegations SET {', '.join(columns)} WHERE id=?",
            params,
        )
        conn.commit()
        return get_delegation(delegation_id)
    finally:
        conn.close()


def _definition_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "rules_profile_ids_json": json.dumps(item.get("rules_profile_ids", [])),
        "permissions_json": json.dumps(item.get("permissions", {})),
        "tools_json": json.dumps(item.get("tools", [])),
        "metadata_json": json.dumps(item.get("metadata", {})),
        "enabled": int(bool(item.get("enabled", True))),
        "built_in": int(bool(item.get("built_in", False))),
        "created_at": item.get("created_at") or now,
        "updated_at": item.get("updated_at") or now,
    }


def _definition(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key, column in JSON_FIELDS.items():
        raw = data.pop(column, None)
        data[key] = (
            json.loads(raw) if raw else ([] if key in {"rules_profile_ids", "tools"} else {})
        )
    data["enabled"] = bool(data["enabled"])
    data["built_in"] = bool(data["built_in"])
    return data


def _delegation_values(item: dict) -> dict:
    now = item.get("created_at") or now_iso()
    return {
        **item,
        "input_json": json.dumps(item.get("input", {})),
        "output_json": json.dumps(item.get("output")) if item.get("output") is not None else None,
        "created_at": item.get("created_at") or now,
        "updated_at": item.get("updated_at") or now,
    }


def _delegation(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_input = data.pop("input_json", None)
    raw_output = data.pop("output_json", None)
    data["input"] = json.loads(raw_input) if raw_input else {}
    data["output"] = json.loads(raw_output) if raw_output else None
    return data
