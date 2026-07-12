"""Audit-only persistence for Provider Runtime."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings


def now() -> str:
    return datetime.now(UTC).isoformat()


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_provider_runtime_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_provider_requests (id TEXT PRIMARY KEY,route_name TEXT,provider_name TEXT,model_name TEXT,request_type TEXT NOT NULL,status TEXT NOT NULL,streaming INTEGER NOT NULL DEFAULT 0,prompt_tokens_estimate INTEGER,completion_tokens_estimate INTEGER,total_tokens_estimate INTEGER,provider_usage_json TEXT,retry_count INTEGER NOT NULL DEFAULT 0,fallback_chain_json TEXT,latency_ms INTEGER,error_category TEXT,error_message TEXT,redaction_summary_json TEXT,metadata_json TEXT,created_at TEXT NOT NULL,completed_at TEXT);
        CREATE TABLE IF NOT EXISTS workspace_provider_health_checks (id TEXT PRIMARY KEY,route_name TEXT,provider_name TEXT,model_name TEXT,status TEXT NOT NULL,latency_ms INTEGER,error_category TEXT,error_message TEXT,checked_at TEXT NOT NULL,metadata_json TEXT);
        CREATE TABLE IF NOT EXISTS workspace_provider_rate_limits (id TEXT PRIMARY KEY,route_name TEXT NOT NULL,provider_name TEXT,model_name TEXT,window_start TEXT NOT NULL,window_seconds INTEGER NOT NULL,request_count INTEGER NOT NULL DEFAULT 0,token_count INTEGER NOT NULL DEFAULT 0,blocked_count INTEGER NOT NULL DEFAULT 0,metadata_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_provider_requests_route ON workspace_provider_requests(route_name,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_provider_requests_provider ON workspace_provider_requests(provider_name,model_name);
        CREATE INDEX IF NOT EXISTS idx_provider_requests_status ON workspace_provider_requests(status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_provider_requests_error ON workspace_provider_requests(error_category);
        CREATE INDEX IF NOT EXISTS idx_provider_health_checked ON workspace_provider_health_checks(checked_at DESC);
        CREATE INDEX IF NOT EXISTS idx_provider_health_route ON workspace_provider_health_checks(route_name,status);
        CREATE INDEX IF NOT EXISTS idx_provider_limits_route ON workspace_provider_rate_limits(route_name,window_start);
        """)
        conn.commit()
    finally:
        conn.close()


def _decode(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for name in ("provider_usage", "fallback_chain", "redaction_summary", "metadata"):
        key = f"{name}_json"
        if key in item:
            item[name] = json.loads(item.pop(key) or ("[]" if name == "fallback_chain" else "{}"))
    item["streaming"] = bool(item.get("streaming", 0))
    return item


def create_request(value: dict) -> dict:
    payload = {
        "id": str(uuid.uuid4()),
        "route_name": value.get("route_name"),
        "provider_name": value.get("provider_name"),
        "model_name": value.get("model_name"),
        "request_type": value["request_type"],
        "status": value.get("status", "running"),
        "streaming": int(bool(value.get("streaming"))),
        "prompt_tokens_estimate": value.get("prompt_tokens_estimate"),
        "completion_tokens_estimate": value.get("completion_tokens_estimate"),
        "total_tokens_estimate": value.get("total_tokens_estimate"),
        "provider_usage_json": json.dumps(value.get("provider_usage") or {}),
        "retry_count": value.get("retry_count", 0),
        "fallback_chain_json": json.dumps(value.get("fallback_chain") or []),
        "latency_ms": value.get("latency_ms"),
        "error_category": value.get("error_category"),
        "error_message": value.get("error_message"),
        "redaction_summary_json": json.dumps(value.get("redaction_summary") or {}),
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "created_at": now(),
        "completed_at": value.get("completed_at"),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_provider_requests VALUES (:id,:route_name,:provider_name,:model_name,:request_type,:status,:streaming,:prompt_tokens_estimate,:completion_tokens_estimate,:total_tokens_estimate,:provider_usage_json,:retry_count,:fallback_chain_json,:latency_ms,:error_category,:error_message,:redaction_summary_json,:metadata_json,:created_at,:completed_at)",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload) or {}


def get_request(request_id: str) -> dict | None:
    conn = _connect()
    try:
        return _decode(
            conn.execute(
                "SELECT * FROM workspace_provider_requests WHERE id=?", (request_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def update_request(request_id: str, **fields: Any) -> dict | None:
    json_fields = {"provider_usage", "fallback_chain", "redaction_summary", "metadata"}
    values = {
        f"{k}_json" if k in json_fields else k: json.dumps(v) if k in json_fields else v
        for k, v in fields.items()
    }
    if not values:
        return get_request(request_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_provider_requests SET {','.join(f'{k}=?' for k in values)} WHERE id=?",
            [*values.values(), request_id],
        )
        conn.commit()
    finally:
        conn.close()
    return get_request(request_id)


def list_requests(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        return [
            _decode(row) or {}
            for row in conn.execute(
                "SELECT * FROM workspace_provider_requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    finally:
        conn.close()


def add_health(value: dict) -> dict:
    payload = {
        "id": str(uuid.uuid4()),
        "route_name": value.get("route_name"),
        "provider_name": value.get("provider_name"),
        "model_name": value.get("model_name"),
        "status": value["status"],
        "latency_ms": value.get("latency_ms"),
        "error_category": value.get("error_category"),
        "error_message": value.get("error_message"),
        "checked_at": now(),
        "metadata_json": json.dumps(value.get("metadata") or {}),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_provider_health_checks VALUES (:id,:route_name,:provider_name,:model_name,:status,:latency_ms,:error_category,:error_message,:checked_at,:metadata_json)",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload) or {}


def list_health(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        return [
            _decode(row) or {}
            for row in conn.execute(
                "SELECT * FROM workspace_provider_health_checks ORDER BY checked_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    finally:
        conn.close()


def rate_records(route_name: str) -> list[dict]:
    conn = _connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM workspace_provider_rate_limits WHERE route_name=?", (route_name,)
            ).fetchall()
        ]
    finally:
        conn.close()


def record_rate(
    route_name: str, provider: str | None, model: str | None, tokens: int, blocked: bool
) -> None:
    from app.services.provider_runtime.rate_limits import window_key

    conn = _connect()
    try:
        for seconds in (60, 86400):
            start = window_key(seconds)
            row = conn.execute(
                "SELECT id FROM workspace_provider_rate_limits WHERE route_name=? AND window_start=? AND window_seconds=?",
                (route_name, start, seconds),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE workspace_provider_rate_limits SET request_count=request_count+1,token_count=token_count+?,blocked_count=blocked_count+? WHERE id=?",
                    (tokens, int(blocked), row["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO workspace_provider_rate_limits VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        route_name,
                        provider,
                        model,
                        start,
                        seconds,
                        1,
                        tokens,
                        int(blocked),
                        "{}",
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def list_rates() -> list[dict]:
    conn = _connect()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM workspace_provider_rate_limits ORDER BY window_start DESC"
            ).fetchall()
        ]
    finally:
        conn.close()
