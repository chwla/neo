from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings

# ruff: noqa: E501


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now() -> str:
    return datetime.now(UTC).isoformat()


def json_text(value: Any) -> str:
    """Serialize controlled service metadata for a SQL column."""
    return json.dumps(value or {})


def initialize_web_search_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_web_search_runs (id TEXT PRIMARY KEY,query_text TEXT NOT NULL,mode TEXT NOT NULL,status TEXT NOT NULL,plan_json TEXT,provider_json TEXT,summary_text TEXT,error TEXT,created_by TEXT,created_at TEXT NOT NULL,completed_at TEXT);
        CREATE TABLE IF NOT EXISTS workspace_web_sources (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,url TEXT NOT NULL,canonical_url TEXT,title TEXT,domain TEXT,snippet TEXT,fetched_text TEXT,fetched_at TEXT,source_type TEXT,credibility_score REAL,freshness_score REAL,relevance_score REAL,final_score REAL,metadata_json TEXT,redaction_summary_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_web_evidence (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,source_id TEXT NOT NULL,claim TEXT NOT NULL,evidence_text TEXT NOT NULL,citation_label TEXT,confidence REAL,metadata_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_web_conflicts (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,topic TEXT NOT NULL,claim_a TEXT NOT NULL,claim_b TEXT NOT NULL,source_ids_json TEXT NOT NULL,severity TEXT,metadata_json TEXT,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_web_source_cache (id TEXT PRIMARY KEY,canonical_url TEXT NOT NULL UNIQUE,title TEXT,domain TEXT,fetched_text TEXT,fetched_at TEXT NOT NULL,etag TEXT,last_modified TEXT,metadata_json TEXT,redaction_summary_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_web_source_run ON workspace_web_sources(run_id,final_score DESC);
        CREATE INDEX IF NOT EXISTS idx_web_evidence_run ON workspace_web_evidence(run_id);
        CREATE INDEX IF NOT EXISTS idx_web_conflict_run ON workspace_web_conflicts(run_id);
        """)
        conn.commit()
    finally:
        conn.close()


def _decode(row: sqlite3.Row | dict | None) -> dict | None:
    if not row:
        return None
    value = dict(row)
    for key in ("plan", "provider", "metadata", "redaction_summary", "source_ids"):
        json_key = f"{key}_json"
        if json_key in value:
            value[key] = json.loads(value.pop(json_key) or ("[]" if key == "source_ids" else "{}"))
    return value


def create_run(query: str, mode: str, plan: dict, provider: dict, status: str = "running") -> dict:
    value = {
        "id": str(uuid.uuid4()),
        "query_text": query,
        "mode": mode,
        "status": status,
        "plan_json": json.dumps(plan),
        "provider_json": json.dumps(provider),
        "summary_text": None,
        "error": None,
        "created_by": "user",
        "created_at": now(),
        "completed_at": None,
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_web_search_runs VALUES (:id,:query_text,:mode,:status,:plan_json,:provider_json,:summary_text,:error,:created_by,:created_at,:completed_at)",
            value,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(value) or {}


def update_run(run_id: str, **fields: Any) -> dict | None:
    if not fields:
        return get_run(run_id)
    conn = _connect()
    try:
        columns = ",".join(f"{key}=?" for key in fields)
        conn.execute(
            f"UPDATE workspace_web_search_runs SET {columns} WHERE id=?", [*fields.values(), run_id]
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id)


def get_run(run_id: str) -> dict | None:
    conn = _connect()
    try:
        return _decode(
            conn.execute("SELECT * FROM workspace_web_search_runs WHERE id=?", (run_id,)).fetchone()
        )
    finally:
        conn.close()


def list_runs(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        return [
            _decode(row) or {}
            for row in conn.execute(
                "SELECT * FROM workspace_web_search_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]
    finally:
        conn.close()


def add_source(run_id: str, value: dict) -> dict:
    value = {
        **value,
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "redaction_summary_json": json.dumps(value.get("redaction_summary") or {}),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_web_sources VALUES (:id,:run_id,:url,:canonical_url,:title,:domain,:snippet,:fetched_text,:fetched_at,:source_type,:credibility_score,:freshness_score,:relevance_score,:final_score,:metadata_json,:redaction_summary_json,:created_at)",
            value,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(value) or {}


def update_source(source_id: str, **fields: Any) -> dict | None:
    if not fields:
        return None
    conn = _connect()
    try:
        columns = ",".join(f"{key}=?" for key in fields)
        conn.execute(
            f"UPDATE workspace_web_sources SET {columns} WHERE id=?", [*fields.values(), source_id]
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM workspace_web_sources WHERE id=?", (source_id,)
        ).fetchone()
        return _decode(row)
    finally:
        conn.close()


def add_evidence(run_id: str, source_id: str, value: dict) -> dict:
    value = {
        **value,
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "source_id": source_id,
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_web_evidence VALUES (:id,:run_id,:source_id,:claim,:evidence_text,:citation_label,:confidence,:metadata_json,:created_at)",
            value,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(value) or {}


def related(table: str, run_id: str) -> list[dict]:
    allowed = {"workspace_web_sources", "workspace_web_evidence", "workspace_web_conflicts"}
    if table not in allowed:
        raise ValueError("Unsupported web search relation.")
    conn = _connect()
    try:
        return [
            _decode(row) or {}
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def cache_list() -> list[dict]:
    conn = _connect()
    try:
        return [
            _decode(row) or {}
            for row in conn.execute(
                "SELECT * FROM workspace_web_source_cache ORDER BY fetched_at DESC"
            ).fetchall()
        ]
    finally:
        conn.close()


def get_cache(canonical_url: str) -> dict | None:
    """Return an existing safe cache entry; callers never receive raw request headers."""
    conn = _connect()
    try:
        return _decode(
            conn.execute(
                "SELECT * FROM workspace_web_source_cache WHERE canonical_url=?",
                (canonical_url,),
            ).fetchone()
        )
    finally:
        conn.close()


def delete_cache(cache_id: str) -> bool:
    conn = _connect()
    try:
        count = conn.execute(
            "DELETE FROM workspace_web_source_cache WHERE id=?", (cache_id,)
        ).rowcount
        conn.commit()
        return bool(count)
    finally:
        conn.close()


def add_conflict(run_id: str, value: dict) -> dict:
    payload = {
        **value,
        "id": str(uuid.uuid4()),
        "run_id": run_id,
        "source_ids_json": json.dumps(value.get("source_ids") or []),
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_web_conflicts VALUES (:id,:run_id,:topic,:claim_a,:claim_b,:source_ids_json,:severity,:metadata_json,:created_at)",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload) or {}


def upsert_cache(value: dict) -> None:
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM workspace_web_source_cache WHERE canonical_url=?",
            (value["canonical_url"],),
        ).fetchone()
        payload = {
            **value,
            "id": existing["id"] if existing else str(uuid.uuid4()),
            "fetched_at": now(),
            "metadata_json": json.dumps(value.get("metadata") or {}),
            "redaction_summary_json": json.dumps(value.get("redaction_summary") or {}),
        }
        if existing:
            conn.execute(
                "UPDATE workspace_web_source_cache SET title=:title,domain=:domain,fetched_text=:fetched_text,fetched_at=:fetched_at,metadata_json=:metadata_json,redaction_summary_json=:redaction_summary_json WHERE id=:id",
                payload,
            )
        else:
            conn.execute(
                "INSERT INTO workspace_web_source_cache VALUES (:id,:canonical_url,:title,:domain,:fetched_text,:fetched_at,NULL,NULL,:metadata_json,:redaction_summary_json)",
                payload,
            )
        conn.commit()
    finally:
        conn.close()
