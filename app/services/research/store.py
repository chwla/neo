"""SQLite-backed persistence for research jobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import get_settings


def _db_path() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def initialize_research_tables() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS research_jobs (
                id TEXT PRIMARY KEY,
                user_query TEXT NOT NULL,
                depth TEXT NOT NULL DEFAULT 'standard',
                max_sources INTEGER NOT NULL DEFAULT 10,
                max_rounds INTEGER NOT NULL DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                progress_percent INTEGER NOT NULL DEFAULT 0,
                current_step TEXT NOT NULL DEFAULT '',
                plan_json TEXT,
                generated_queries_json TEXT,
                sources_json TEXT,
                evidence_json TEXT,
                report TEXT NOT NULL DEFAULT '',
                error TEXT,
                metadata_json TEXT,
                progress_log_json TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_jobs_status
            ON research_jobs(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_research_jobs_created
            ON research_jobs(created_at DESC)
        """)
        conn.commit()
    finally:
        conn.close()


def save_job(job_dict: dict) -> None:
    conn = _connect()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO research_jobs (
                id, user_query, depth, max_sources, max_rounds,
                status, created_at, updated_at,
                progress_percent, current_step,
                plan_json, generated_queries_json, sources_json,
                evidence_json, report, error,
                metadata_json, progress_log_json
            ) VALUES (
                :id, :user_query, :depth, :max_sources, :max_rounds,
                :status, :created_at, :updated_at,
                :progress_percent, :current_step,
                :plan_json, :generated_queries_json, :sources_json,
                :evidence_json, :report, :error,
                :metadata_json, :progress_log_json
            )
            ON CONFLICT(id) DO UPDATE SET
                user_query = excluded.user_query,
                depth = excluded.depth,
                max_sources = excluded.max_sources,
                max_rounds = excluded.max_rounds,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                progress_percent = excluded.progress_percent,
                current_step = excluded.current_step,
                plan_json = excluded.plan_json,
                generated_queries_json = excluded.generated_queries_json,
                sources_json = excluded.sources_json,
                evidence_json = excluded.evidence_json,
                report = excluded.report,
                error = excluded.error,
                metadata_json = excluded.metadata_json,
                progress_log_json = excluded.progress_log_json
            """,
            {
                "id": job_dict["id"],
                "user_query": job_dict["user_query"],
                "depth": job_dict.get("depth", "standard"),
                "max_sources": job_dict.get("max_sources", 10),
                "max_rounds": job_dict.get("max_rounds", 2),
                "status": job_dict.get("status", "queued"),
                "created_at": job_dict.get("created_at", now),
                "updated_at": now,
                "progress_percent": job_dict.get("progress_percent", 0),
                "current_step": job_dict.get("current_step", ""),
                "plan_json": json.dumps(job_dict.get("plan")) if job_dict.get("plan") else None,
                "generated_queries_json": json.dumps(job_dict.get("generated_queries", [])),
                "sources_json": json.dumps(job_dict.get("sources", [])),
                "evidence_json": json.dumps(job_dict.get("evidence_chunks", [])),
                "report": job_dict.get("report", ""),
                "error": job_dict.get("error"),
                "metadata_json": json.dumps(job_dict.get("metadata", {})),
                "progress_log_json": json.dumps(job_dict.get("progress_log", [])),
            },
        )
        conn.commit()
    finally:
        conn.close()


def load_job(job_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM research_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return _row_to_dict(row)
    finally:
        conn.close()


def list_jobs(limit: int = 20, offset: int = 0) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM research_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        conn.close()


def update_job_status(job_id: str, status: str, **kwargs) -> None:
    conn = _connect()
    try:
        now = datetime.now(timezone.utc).isoformat()
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, now]
        for key, val in kwargs.items():
            if key in ("progress_percent", "current_step", "report", "error"):
                sets.append(f"{key} = ?")
                vals.append(val)
            elif key.endswith("_json"):
                sets.append(f"{key} = ?")
                vals.append(json.dumps(val) if val is not None else None)
        vals.append(job_id)
        conn.execute(f"UPDATE research_jobs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()


def clear_all_jobs() -> int:
    conn = _connect()
    try:
        cursor = conn.execute("DELETE FROM research_jobs")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for json_col in ("plan_json", "generated_queries_json", "sources_json", "evidence_json", "metadata_json", "progress_log_json"):
        base = json_col.replace("_json", "")
        raw = d.pop(json_col, None)
        d[base] = json.loads(raw) if raw else ([] if base in ("generated_queries", "sources", "evidence_chunks", "progress_log") else None if base == "plan" else {})
    if "evidence" in d:
        d["evidence_chunks"] = d.pop("evidence")
    return d
