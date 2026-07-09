from __future__ import annotations

import json
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


def initialize_rule_tables() -> None:
    with _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_rule_profiles (
            id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT, name TEXT NOT NULL,
            description TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            priority INTEGER NOT NULL DEFAULT 100,
            rules_json TEXT NOT NULL, source_type TEXT NOT NULL DEFAULT 'ui', source_path TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_rule_resolution_logs (
            id TEXT PRIMARY KEY, context_type TEXT NOT NULL, context_id TEXT, project_id TEXT,
            task_id TEXT, repo_id TEXT, applied_profiles_json TEXT NOT NULL,
            resolved_rules_json TEXT NOT NULL, warnings_json TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_rule_profiles_scope
        ON workspace_rule_profiles(scope_type, scope_id, enabled, priority);
        CREATE INDEX IF NOT EXISTS idx_workspace_rule_resolution_logs_context
        ON workspace_rule_resolution_logs(context_type, context_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workspace_rule_resolution_logs_repo
        ON workspace_rule_resolution_logs(repo_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workspace_rule_resolution_logs_task
        ON workspace_rule_resolution_logs(task_id, created_at);
        """)


def _profile(row) -> dict:
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    try:
        item["rules"] = json.loads(item.pop("rules_json"))
    except (TypeError, json.JSONDecodeError):
        item["rules"] = {}
    return item


def insert_profile(item: dict) -> dict:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO workspace_rule_profiles
        (id,scope_type,scope_id,name,description,enabled,priority,rules_json,source_type,source_path,created_at,updated_at)
            VALUES (:id,:scope_type,:scope_id,:name,:description,:enabled,:priority,
            :rules_json,:source_type,:source_path,:created_at,:updated_at)""",
            {
                **item,
                "enabled": int(item.get("enabled", True)),
                "rules_json": json.dumps(item.get("rules", {}), sort_keys=True),
            },
        )
    return get_profile(item["id"])


def get_profile(profile_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workspace_rule_profiles WHERE id=?", (profile_id,)
        ).fetchone()
    return _profile(row) if row else None


def find_source_profile(repo_id: str, source_path: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM workspace_rule_profiles WHERE scope_type='repo' "
            "AND scope_id=? AND source_path=?",
            (repo_id, source_path),
        ).fetchone()
    return _profile(row) if row else None


def list_profiles(*, scope_type=None, scope_id=None, enabled=None, limit=200, offset=0):
    where, params = ["1=1"], []
    for key, value in (("scope_type", scope_type), ("scope_id", scope_id)):
        if value is not None:
            where.append(f"{key}=?")
            params.append(value)
    if enabled is not None:
        where.append("enabled=?")
        params.append(int(enabled))
    clause = " AND ".join(where)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_rule_profiles WHERE {clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM workspace_rule_profiles WHERE {clause} "
            "ORDER BY priority, created_at, id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    return [_profile(row) for row in rows], int(total)


def update_profile(profile_id: str, updates: dict) -> dict | None:
    allowed = {
        "scope_type",
        "scope_id",
        "name",
        "description",
        "enabled",
        "priority",
        "rules",
        "source_type",
        "source_path",
        "updated_at",
    }
    columns, values = [], []
    for key, value in updates.items():
        if key not in allowed:
            continue
        column = "rules_json" if key == "rules" else key
        if key == "rules":
            value = json.dumps(value, sort_keys=True)
        if key == "enabled":
            value = int(value)
        columns.append(f"{column}=?")
        values.append(value)
    if columns:
        with _connect() as conn:
            conn.execute(
                f"UPDATE workspace_rule_profiles SET {', '.join(columns)} WHERE id=?",
                [*values, profile_id],
            )
    return get_profile(profile_id)


def insert_log(item: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO workspace_rule_resolution_logs
        (id,context_type,context_id,project_id,task_id,repo_id,applied_profiles_json,resolved_rules_json,warnings_json,created_at)
            VALUES (:id,:context_type,:context_id,:project_id,:task_id,:repo_id,
            :applied_profiles_json,:resolved_rules_json,:warnings_json,:created_at)""",
            {
                **item,
                "applied_profiles_json": json.dumps(item["applied_profiles"]),
                "resolved_rules_json": json.dumps(item["resolved_rules"], sort_keys=True),
                "warnings_json": json.dumps(item.get("warnings", [])),
            },
        )


def list_logs(
    *, context_type=None, context_id=None, repo_id=None, task_id=None, limit=100, offset=0
):
    where, params = ["1=1"], []
    for key, value in (
        ("context_type", context_type),
        ("context_id", context_id),
        ("repo_id", repo_id),
        ("task_id", task_id),
    ):
        if value:
            where.append(f"{key}=?")
            params.append(value)
    clause = " AND ".join(where)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM workspace_rule_resolution_logs WHERE {clause}", params
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM workspace_rule_resolution_logs WHERE {clause} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key, column in (
            ("applied_profiles", "applied_profiles_json"),
            ("resolved_rules", "resolved_rules_json"),
            ("warnings", "warnings_json"),
        ):
            try:
                item[key] = json.loads(item.pop(column) or "[]")
            except json.JSONDecodeError:
                item[key] = [] if key != "resolved_rules" else {}
        items.append(item)
    return items, int(total)
