"""SQLite persistence for research runs, evidence, claims, reports, and audit data."""
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
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize_research_mode_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_research_runs (
          id TEXT PRIMARY KEY, question TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
          plan_json TEXT, memory_retrieval_ids_json TEXT, web_search_run_ids_json TEXT,
          report_text TEXT, executive_summary TEXT, confidence_json TEXT, uncertainty_json TEXT,
          created_by TEXT, created_at TEXT NOT NULL, completed_at TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS workspace_research_claims (
          id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, claim TEXT NOT NULL, claim_type TEXT,
          confidence REAL, citation_ids_json TEXT NOT NULL, evidence_ids_json TEXT NOT NULL,
          status TEXT NOT NULL, metadata_json TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY (research_run_id) REFERENCES workspace_research_runs(id)
        );
        CREATE TABLE IF NOT EXISTS workspace_research_evidence (
          id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT,
          citation_label TEXT, evidence_text TEXT NOT NULL, extracted_claim TEXT, confidence REAL,
          quality_score REAL, metadata_json TEXT, created_at TEXT NOT NULL,
          FOREIGN KEY (research_run_id) REFERENCES workspace_research_runs(id)
        );
        CREATE TABLE IF NOT EXISTS workspace_research_reports (
          id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, title TEXT NOT NULL, report_format TEXT NOT NULL,
          content_text TEXT NOT NULL, sections_json TEXT, citations_json TEXT, confidence_json TEXT,
          created_at TEXT NOT NULL, FOREIGN KEY (research_run_id) REFERENCES workspace_research_runs(id)
        );
        CREATE TABLE IF NOT EXISTS workspace_research_conflicts (
          id TEXT PRIMARY KEY, research_run_id TEXT NOT NULL, topic TEXT NOT NULL, conflict_type TEXT NOT NULL,
          claims_json TEXT, sources_json TEXT, severity TEXT NOT NULL, recommended_resolution TEXT NOT NULL,
          created_at TEXT NOT NULL, FOREIGN KEY (research_run_id) REFERENCES workspace_research_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_research_claim_run ON workspace_research_claims(research_run_id);
        CREATE INDEX IF NOT EXISTS idx_research_claim_status ON workspace_research_claims(status);
        CREATE INDEX IF NOT EXISTS idx_research_claim_confidence ON workspace_research_claims(confidence);
        CREATE INDEX IF NOT EXISTS idx_research_claim_type ON workspace_research_claims(claim_type);
        CREATE INDEX IF NOT EXISTS idx_research_evidence_run ON workspace_research_evidence(research_run_id);
        CREATE INDEX IF NOT EXISTS idx_research_report_run ON workspace_research_reports(research_run_id);
        CREATE INDEX IF NOT EXISTS idx_research_runs_status ON workspace_research_runs(status);
        CREATE INDEX IF NOT EXISTS idx_research_runs_created ON workspace_research_runs(created_at DESC);
        """)
        conn.commit()
    finally:
        conn.close()


def _decode(value: dict) -> dict:
    for key in (
        "plan",
        "memory_retrieval_ids",
        "web_search_run_ids",
        "confidence",
        "uncertainty",
        "citation_ids",
        "evidence_ids",
        "metadata",
        "sections",
        "citations",
        "claims",
        "sources",
    ):
        json_key = f"{key}_json"
        if json_key in value:
            raw = value.pop(json_key)
            value[key] = json.loads(
                raw
                or (
                    "[]"
                    if key
                    in {
                        "memory_retrieval_ids",
                        "web_search_run_ids",
                        "citation_ids",
                        "evidence_ids",
                        "citations",
                        "claims",
                        "sources",
                    }
                    else "{}"
                )
            )
    return value


def create_run(request: dict, plan: dict) -> dict:
    value = {
        "id": str(uuid.uuid4()),
        "question": request["question"],
        "mode": request["mode"],
        "status": "planning",
        "plan_json": json.dumps(plan),
        "memory_retrieval_ids_json": "[]",
        "web_search_run_ids_json": "[]",
        "report_text": None,
        "executive_summary": None,
        "confidence_json": "{}",
        "uncertainty_json": "[]",
        "created_by": request.get("created_by", "user"),
        "created_at": now(),
        "completed_at": None,
        "error": None,
    }
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_research_runs VALUES (:id,:question,:mode,:status,:plan_json,:memory_retrieval_ids_json,:web_search_run_ids_json,:report_text,:executive_summary,:confidence_json,:uncertainty_json,:created_by,:created_at,:completed_at,:error)""",
            value,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(value)


def get_run(run_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM workspace_research_runs WHERE id=?", (run_id,)).fetchone()
        return _decode(dict(row)) if row else None
    finally:
        conn.close()


def list_runs(limit: int = 100) -> list[dict]:
    conn = _connect()
    try:
        return [
            _decode(dict(row))
            for row in conn.execute(
                "SELECT * FROM workspace_research_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        ]
    finally:
        conn.close()


def update_run(run_id: str, **fields: Any) -> dict | None:
    if not fields:
        return get_run(run_id)
    allowed = {
        "status",
        "plan_json",
        "memory_retrieval_ids_json",
        "web_search_run_ids_json",
        "report_text",
        "executive_summary",
        "confidence_json",
        "uncertainty_json",
        "completed_at",
        "error",
    }
    fields = {
        key: json.dumps(value) if key.endswith("_json") and not isinstance(value, str) else value
        for key, value in fields.items()
        if key in allowed
    }
    if not fields:
        return get_run(run_id)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE workspace_research_runs SET {','.join(f'{key}=?' for key in fields)} WHERE id=?",
            [*fields.values(), run_id],
        )
        conn.commit()
    finally:
        conn.close()
    return get_run(run_id)


def add_evidence(run_id: str, value: dict) -> dict:
    payload = {
        **value,
        "id": str(uuid.uuid4()),
        "research_run_id": run_id,
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_research_evidence VALUES (:id,:research_run_id,:source_type,:source_id,:citation_label,:evidence_text,:extracted_claim,:confidence,:quality_score,:metadata_json,:created_at)""",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload)


def add_claim(run_id: str, value: dict) -> dict:
    payload = {
        **value,
        "id": str(uuid.uuid4()),
        "research_run_id": run_id,
        "citation_ids_json": json.dumps(value.get("citation_ids") or []),
        "evidence_ids_json": json.dumps(value.get("evidence_ids") or []),
        "metadata_json": json.dumps(value.get("metadata") or {}),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_research_claims VALUES (:id,:research_run_id,:claim,:claim_type,:confidence,:citation_ids_json,:evidence_ids_json,:status,:metadata_json,:created_at)""",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload)


def add_conflict(run_id: str, value: dict) -> dict:
    payload = {
        **value,
        "id": str(uuid.uuid4()),
        "research_run_id": run_id,
        "claims_json": json.dumps(value.get("claims") or []),
        "sources_json": json.dumps(value.get("sources") or []),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_research_conflicts VALUES (:id,:research_run_id,:topic,:conflict_type,:claims_json,:sources_json,:severity,:recommended_resolution,:created_at)""",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload)


def add_report(
    run_id: str, title: str, content: str, sections: dict, citations: list[str], confidence: dict
) -> dict:
    payload = {
        "id": str(uuid.uuid4()),
        "research_run_id": run_id,
        "title": title,
        "report_format": "markdown",
        "content_text": content,
        "sections_json": json.dumps(sections),
        "citations_json": json.dumps(citations),
        "confidence_json": json.dumps(confidence),
        "created_at": now(),
    }
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO workspace_research_reports VALUES (:id,:research_run_id,:title,:report_format,:content_text,:sections_json,:citations_json,:confidence_json,:created_at)""",
            payload,
        )
        conn.commit()
    finally:
        conn.close()
    return _decode(payload)


def related(table: str, run_id: str) -> list[dict]:
    allowed = {
        "workspace_research_evidence",
        "workspace_research_claims",
        "workspace_research_reports",
        "workspace_research_conflicts",
    }
    if table not in allowed:
        raise ValueError("Unsupported research relation.")
    conn = _connect()
    try:
        return [
            _decode(dict(row))
            for row in conn.execute(
                f"SELECT * FROM {table} WHERE research_run_id=? ORDER BY created_at", (run_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def delete_run(run_id: str) -> bool:
    conn = _connect()
    try:
        for table in (
            "workspace_research_reports",
            "workspace_research_conflicts",
            "workspace_research_claims",
            "workspace_research_evidence",
            "workspace_research_runs",
        ):
            conn.execute(
                f"DELETE FROM {table} WHERE {'research_run_id' if table != 'workspace_research_runs' else 'id'}=?",
                (run_id,),
            )
        conn.commit()
        return True
    finally:
        conn.close()
