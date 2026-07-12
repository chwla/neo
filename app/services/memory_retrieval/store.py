from __future__ import annotations

# ruff: noqa: E501  # SQL statements are kept legible as complete SQL clauses.
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings


def _connect() -> sqlite3.Connection:
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def initialize_memory_retrieval_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_memory_items (
          id TEXT PRIMARY KEY, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL,
          source_type TEXT NOT NULL, source_id TEXT, memory_type TEXT NOT NULL,
          title TEXT NOT NULL, content_text TEXT NOT NULL, content_json TEXT,
          tags_json TEXT, importance INTEGER NOT NULL DEFAULT 3, confidence REAL NOT NULL DEFAULT 1.0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_accessed_at TEXT,
          access_count INTEGER NOT NULL DEFAULT 0, expires_at TEXT, redaction_summary_json TEXT
        );
        CREATE TABLE IF NOT EXISTS workspace_memory_links (
          id TEXT PRIMARY KEY, source_memory_id TEXT NOT NULL, target_type TEXT NOT NULL,
          target_id TEXT NOT NULL, relation TEXT NOT NULL, metadata_json TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY (source_memory_id) REFERENCES workspace_memory_items(id)
        );
        CREATE TABLE IF NOT EXISTS workspace_memory_retrievals (
          id TEXT PRIMARY KEY, query_text TEXT NOT NULL, scope_type TEXT, scope_id TEXT,
          filters_json TEXT, results_json TEXT NOT NULL, scorer_json TEXT, created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS workspace_memory_items_fts
        USING fts5(memory_id UNINDEXED, title, content_text, tags_text);
        CREATE INDEX IF NOT EXISTS idx_memory_scope ON workspace_memory_items(scope_type, scope_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_source ON workspace_memory_items(source_type, source_id);
        CREATE INDEX IF NOT EXISTS idx_memory_type ON workspace_memory_items(memory_type);
        CREATE INDEX IF NOT EXISTS idx_memory_importance ON workspace_memory_items(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_created ON workspace_memory_items(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memory_access ON workspace_memory_items(last_accessed_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe ON workspace_memory_items(scope_type, scope_id, source_type, source_id, memory_type, title);
        """)
        conn.commit()
    finally:
        conn.close()


def _item(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["content_json"] = json.loads(item["content_json"] or "{}")
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    item["redaction_summary"] = json.loads(item.pop("redaction_summary_json") or "{}")
    return item


def get_item(item_id: str) -> dict | None:
    conn = _connect()
    try:
        return _item(
            conn.execute("SELECT * FROM workspace_memory_items WHERE id=?", (item_id,)).fetchone()
        )
    finally:
        conn.close()


def upsert_item(values: dict[str, Any]) -> dict:
    from app.services.memory_retrieval.redaction import redact_memory

    redacted, redaction_summary = redact_memory(
        {
            "title": values.get("title", ""),
            "content_text": values.get("content_text", ""),
            "content_json": values.get("content_json") or {},
            "tags": values.get("tags") or [],
        }
    )
    prior_summary = values.get("redaction_summary") or {}
    redaction_summary = {
        "credential_or_env_redactions": max(
            int(prior_summary.get("credential_or_env_redactions", 0)),
            int(redaction_summary.get("credential_or_env_redactions", 0)),
        ),
        "redacted": bool(prior_summary.get("redacted") or redaction_summary.get("redacted")),
    }
    now = now_iso()
    values = {
        **values,
        **redacted,
        "id": values.get("id") or str(uuid.uuid4()),
        "created_at": values.get("created_at") or now,
        "updated_at": now,
        "expires_at": values.get("expires_at"),
        "redaction_summary": redaction_summary,
    }
    values["content_json"] = json.dumps(values.get("content_json") or {})
    values["tags_json"] = json.dumps(values.get("tags") or [])
    values["redaction_summary_json"] = json.dumps(values.get("redaction_summary") or {})
    conn = _connect()
    try:
        duplicate = conn.execute(
            "SELECT id, created_at FROM workspace_memory_items WHERE scope_type=? AND scope_id=? AND source_type=? AND COALESCE(source_id,'')=COALESCE(?, '') AND memory_type=? AND title=?",
            (
                values["scope_type"],
                values["scope_id"],
                values["source_type"],
                values.get("source_id"),
                values["memory_type"],
                values["title"],
            ),
        ).fetchone()
        if duplicate:
            values["id"], values["created_at"] = duplicate["id"], duplicate["created_at"]
            conn.execute(
                """UPDATE workspace_memory_items SET content_text=:content_text, content_json=:content_json, tags_json=:tags_json, importance=:importance, confidence=:confidence, updated_at=:updated_at, expires_at=:expires_at, redaction_summary_json=:redaction_summary_json WHERE id=:id""",
                values,
            )
            conn.execute(
                "DELETE FROM workspace_memory_items_fts WHERE memory_id=?", (values["id"],)
            )
        else:
            conn.execute(
                """INSERT INTO workspace_memory_items (id,scope_type,scope_id,source_type,source_id,memory_type,title,content_text,content_json,tags_json,importance,confidence,created_at,updated_at,last_accessed_at,access_count,expires_at,redaction_summary_json) VALUES (:id,:scope_type,:scope_id,:source_type,:source_id,:memory_type,:title,:content_text,:content_json,:tags_json,:importance,:confidence,:created_at,:updated_at,NULL,0,:expires_at,:redaction_summary_json)""",
                values,
            )
        conn.execute(
            "INSERT INTO workspace_memory_items_fts(memory_id,title,content_text,tags_text) VALUES (?,?,?,?)",
            (
                values["id"],
                values["title"],
                values["content_text"],
                " ".join(values.get("tags") or []),
            ),
        )
        conn.commit()
        return get_item(values["id"]) or {}
    finally:
        conn.close()


def update_item(item_id: str, fields: dict[str, Any]) -> dict | None:
    item = get_item(item_id)
    if not item:
        return None
    return upsert_item({**item, **fields, "id": item_id, "created_at": item["created_at"]})


def delete_item(item_id: str) -> bool:
    conn = _connect()
    try:
        conn.execute("DELETE FROM workspace_memory_items_fts WHERE memory_id=?", (item_id,))
        deleted = conn.execute("DELETE FROM workspace_memory_items WHERE id=?", (item_id,)).rowcount
        conn.commit()
        return bool(deleted)
    finally:
        conn.close()


def link_memory(memory_id: str, target_type: str, target_id: str, relation: str = "derived_from") -> None:
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT id FROM workspace_memory_links WHERE source_memory_id=? AND target_type=? AND target_id=? AND relation=?",
            (memory_id, target_type, target_id, relation),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO workspace_memory_links VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), memory_id, target_type, target_id, relation, "{}", now_iso()),
            )
            conn.commit()
    finally:
        conn.close()


def list_items(
    scope_type: str | None = None, scope_id: str | None = None, limit: int = 100
) -> list[dict]:
    where, params = ["1=1"], []
    if scope_type:
        where.append("scope_type=?")
        params.append(scope_type)
    if scope_id:
        where.append("scope_id=?")
        params.append(scope_id)
    conn = _connect()
    try:
        return [
            _item(row) or {}
            for row in conn.execute(
                f"SELECT * FROM workspace_memory_items WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        ]
    finally:
        conn.close()


def candidates(query: str, limit: int = 250) -> list[dict]:
    terms = [term.replace('"', "") for term in query.split() if term.strip()]
    conn = _connect()
    try:
        rows: list[sqlite3.Row] = []
        if terms:
            try:
                match = " OR ".join(f'"{term}"' for term in terms)
                rows = conn.execute(
                    "SELECT m.* FROM workspace_memory_items_fts f JOIN workspace_memory_items m ON m.id=f.memory_id WHERE workspace_memory_items_fts MATCH ? LIMIT ?",
                    (match, limit),
                ).fetchall()
            except sqlite3.Error:
                rows = []
        if not rows:
            rows = conn.execute(
                "SELECT * FROM workspace_memory_items ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_item(row) or {} for row in rows]
    finally:
        conn.close()


def mark_accessed(ids: list[str]) -> None:
    if not ids:
        return
    conn = _connect()
    try:
        conn.executemany(
            "UPDATE workspace_memory_items SET access_count=access_count+1,last_accessed_at=? WHERE id=?",
            [(now_iso(), item_id) for item_id in ids],
        )
        conn.commit()
    finally:
        conn.close()


def save_retrieval(values: dict[str, Any]) -> dict:
    values = {**values, "id": str(uuid.uuid4()), "created_at": now_iso()}
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO workspace_memory_retrievals VALUES (:id,:query_text,:scope_type,:scope_id,:filters_json,:results_json,:scorer_json,:created_by,:created_at)",
            {
                **values,
                "filters_json": json.dumps(values.get("filters") or {}),
                "results_json": json.dumps(values.get("results") or []),
                "scorer_json": json.dumps(values.get("scorer") or {}),
            },
        )
        conn.commit()
        return get_retrieval(values["id"]) or {}
    finally:
        conn.close()


def _retrieval(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for key in ("filters", "results", "scorer"):
        item[key] = json.loads(item.pop(f"{key}_json") or ("[]" if key == "results" else "{}"))
    return item


def list_retrievals(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        return [
            _retrieval(row) or {}
            for row in conn.execute(
                "SELECT * FROM workspace_memory_retrievals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
    finally:
        conn.close()


def get_retrieval(retrieval_id: str) -> dict | None:
    conn = _connect()
    try:
        return _retrieval(
            conn.execute(
                "SELECT * FROM workspace_memory_retrievals WHERE id=?", (retrieval_id,)
            ).fetchone()
        )
    finally:
        conn.close()


def prune_candidates(stale_before: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM workspace_memory_items WHERE memory_type NOT IN ('user_instruction','safety_note') AND ((expires_at IS NOT NULL AND expires_at < ?) OR (importance <= 1 AND COALESCE(last_accessed_at,updated_at) < ?))",
            (now_iso(), stale_before),
        ).fetchall()
        return [_item(row) or {} for row in rows]
    finally:
        conn.close()
