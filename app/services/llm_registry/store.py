from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_llm_registry_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspace_llm_providers (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, provider_type TEXT NOT NULL,
                base_url TEXT, api_key_ref TEXT, default_model TEXT,
                enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100,
                timeout_seconds INTEGER NOT NULL DEFAULT 60, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace_llm_models (
                id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name TEXT NOT NULL,
                display_name TEXT, capability_json TEXT, context_window INTEGER,
                max_output_tokens INTEGER, supports_tools INTEGER NOT NULL DEFAULT 0,
                supports_json INTEGER NOT NULL DEFAULT 0,
                supports_vision INTEGER NOT NULL DEFAULT 0,
                supports_embeddings INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (provider_id) REFERENCES workspace_llm_providers(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_llm_routes (
                id TEXT PRIMARY KEY, route_name TEXT NOT NULL UNIQUE,
                provider_id TEXT, model_id TEXT, fallback_provider_id TEXT,
                fallback_model_id TEXT, temperature REAL, max_output_tokens INTEGER,
                enabled INTEGER NOT NULL DEFAULT 1, metadata_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (provider_id) REFERENCES workspace_llm_providers(id),
                FOREIGN KEY (model_id) REFERENCES workspace_llm_models(id),
                FOREIGN KEY (fallback_provider_id) REFERENCES workspace_llm_providers(id),
                FOREIGN KEY (fallback_model_id) REFERENCES workspace_llm_models(id)
            );
            CREATE TABLE IF NOT EXISTS workspace_llm_calls (
                id TEXT PRIMARY KEY, route_name TEXT, provider_id TEXT, model_id TEXT,
                status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
                total_tokens INTEGER, latency_ms INTEGER, error TEXT,
                fallback_used INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                FOREIGN KEY (provider_id) REFERENCES workspace_llm_providers(id),
                FOREIGN KEY (model_id) REFERENCES workspace_llm_models(id)
            );
            CREATE INDEX IF NOT EXISTS idx_workspace_llm_providers_type
            ON workspace_llm_providers(provider_type, enabled, priority);
            CREATE INDEX IF NOT EXISTS idx_workspace_llm_models_provider
            ON workspace_llm_models(provider_id, enabled);
            CREATE INDEX IF NOT EXISTS idx_workspace_llm_routes_name
            ON workspace_llm_routes(route_name);
            CREATE INDEX IF NOT EXISTS idx_workspace_llm_calls_created
            ON workspace_llm_calls(created_at);
            CREATE INDEX IF NOT EXISTS idx_workspace_llm_calls_status
            ON workspace_llm_calls(status, created_at);
        """)
        conn.execute(
            "UPDATE workspace_llm_models SET supports_json = 0 "
            "WHERE typeof(supports_json) = 'text'"
        )
        conn.commit()
    finally:
        conn.close()


def _decode(row: sqlite3.Row | None, kind: str) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for source, target in (("metadata_json", "metadata"), ("capability_json", "capabilities")):
        if source in item:
            item[target] = json.loads(item.pop(source) or "{}")
    for key in (
        "enabled",
        "supports_tools",
        "supports_json",
        "supports_vision",
        "supports_embeddings",
        "fallback_used",
    ):
        if key in item:
            item[key] = bool(item[key])
    item["kind"] = kind
    return item


def list_rows(table: str, kind: str, order: str) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        return [
            _decode(row, kind) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]
    finally:
        conn.close()


def get_row(table: str, kind: str, row_id: str, *, key: str = "id") -> dict[str, Any] | None:
    conn = _connect()
    try:
        return _decode(
            conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (row_id,)).fetchone(), kind
        )
    finally:
        conn.close()


def insert_provider(item: dict[str, Any]) -> dict[str, Any]:
    return _insert(
        "workspace_llm_providers",
        (
            "id",
            "name",
            "provider_type",
            "base_url",
            "api_key_ref",
            "default_model",
            "enabled",
            "priority",
            "timeout_seconds",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
        item,
    )


def insert_model(item: dict[str, Any]) -> dict[str, Any]:
    return _insert(
        "workspace_llm_models",
        (
            "id",
            "provider_id",
            "model_name",
            "display_name",
            "capability_json",
            "context_window",
            "max_output_tokens",
            "supports_tools",
            "supports_json",
            "supports_vision",
            "supports_embeddings",
            "enabled",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
        item,
    )


def insert_route(item: dict[str, Any]) -> dict[str, Any]:
    return _insert(
        "workspace_llm_routes",
        (
            "id",
            "route_name",
            "provider_id",
            "model_id",
            "fallback_provider_id",
            "fallback_model_id",
            "temperature",
            "max_output_tokens",
            "enabled",
            "metadata_json",
            "created_at",
            "updated_at",
        ),
        item,
    )


def _insert(table: str, columns: tuple[str, ...], item: dict[str, Any]) -> dict[str, Any]:
    values = []
    for column in columns:
        key = {"metadata_json": "metadata", "capability_json": "capabilities"}.get(column, column)
        value = item.get(key)
        if column in {"metadata_json", "capability_json"}:
            value = json.dumps(value or {})
        elif isinstance(value, bool):
            value = int(value)
        values.append(value)
    conn = _connect()
    try:
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        conn.commit()
        kind = table.removeprefix("workspace_llm_").rstrip("s")
        return get_row(table, kind, item["id"])
    finally:
        conn.close()


def update_row(
    table: str, kind: str, row_id: str, updates: dict[str, Any]
) -> dict[str, Any] | None:
    columns, values = [], []
    for key, value in updates.items():
        column = {"metadata": "metadata_json", "capabilities": "capability_json"}.get(key, key)
        if column in {"metadata_json", "capability_json"}:
            value = json.dumps(value or {})
        elif isinstance(value, bool):
            value = int(value)
        columns.append(f"{column} = ?")
        values.append(value)
    if not columns:
        return get_row(table, kind, row_id)
    conn = _connect()
    try:
        conn.execute(f"UPDATE {table} SET {', '.join(columns)} WHERE id = ?", [*values, row_id])
        conn.commit()
    finally:
        conn.close()
    return get_row(table, kind, row_id)


def delete_row(table: str, row_id: str) -> bool:
    conn = _connect()
    try:
        result = conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        conn.commit()
        return bool(result.rowcount)
    finally:
        conn.close()


def insert_call(item: dict[str, Any]) -> dict[str, Any]:
    columns = (
        "id",
        "route_name",
        "provider_id",
        "model_id",
        "status",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "latency_ms",
        "error",
        "fallback_used",
        "created_at",
    )
    conn = _connect()
    try:
        placeholders = ", ".join("?" for _ in columns)
        values = [
            int(item.get(c, False)) if c == "fallback_used" else item.get(c)
            for c in columns
        ]
        conn.execute(
            f"INSERT INTO workspace_llm_calls ({', '.join(columns)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        return get_row("workspace_llm_calls", "call", item["id"])
    finally:
        conn.close()


def list_calls(*, route_name=None, provider_id=None, status=None, limit=100, offset=0):
    clauses, values = [], []
    for column, value in (
        ("route_name", route_name),
        ("provider_id", provider_id),
        ("status", status),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM workspace_llm_calls{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            [*values, limit, offset],
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) FROM workspace_llm_calls{where}", values).fetchone()[
            0
        ]
        return [_decode(row, "call") for row in rows], total
    finally:
        conn.close()
