"""SQLite-backed persistence for research jobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from app.core.config import get_settings
from app.services.disclosure.labels import Label, content_hash, from_json, join, label_of, to_json

_JSON_COLUMNS = {
    "plan_json": "plan",
    "generated_queries_json": "generated_queries",
    "sources_json": "sources",
    "evidence_json": "evidence",
    "claims_json": "claims",
    "trace_json": "trace",
    "metadata_json": "metadata",
    "progress_log_json": "progress_log",
}
_LIST_FIELDS = {"generated_queries", "sources", "evidence", "claims", "progress_log"}
_NULLABLE_FIELDS = {"plan", "trace"}
_ADDED_COLUMNS = {
    "claims_json": "TEXT",
    "trace_json": "TEXT",
    "query_label_json": "TEXT",
    "query_hash": "TEXT",
}


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
                progress_log_json TEXT,
                claims_json TEXT,
                trace_json TEXT
            )
        """)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(research_jobs)")}
        for column, column_type in _ADDED_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE research_jobs ADD COLUMN {column} {column_type}")
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


def _server_metadata(conn: sqlite3.Connection, job_id: str, candidate: dict | None) -> dict:
    """Preserve the stored label; never take provenance from a caller's metadata."""

    metadata = dict(candidate or {})
    metadata.pop("source_label", None)
    prior = conn.execute(
        "SELECT metadata_json FROM research_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    label = Label.private(f"research:{job_id}")
    if prior is not None:
        try:
            prior_metadata = json.loads(prior["metadata_json"] or "{}")
        except (TypeError, ValueError):
            prior_metadata = {}
        previous = from_json(
            prior_metadata.get("source_label") if isinstance(prior_metadata, dict) else None
        )
        label = join(previous, label)
    metadata["source_label"] = to_json(label)
    return metadata


def save_job(job_dict: dict, *, query_label: Label | None = None) -> None:
    conn = _connect()
    try:
        now = datetime.now(UTC).isoformat()
        job_id = str(job_dict["id"])
        is_new = (
            conn.execute("SELECT 1 FROM research_jobs WHERE id = ?", (job_id,)).fetchone() is None
        )
        metadata = _server_metadata(conn, job_id, job_dict.get("metadata"))
        conn.execute(
            """
            INSERT INTO research_jobs (
                id, user_query, depth, max_sources, max_rounds,
                status, created_at, updated_at,
                progress_percent, current_step,
                plan_json, generated_queries_json, sources_json,
                evidence_json, claims_json, trace_json, report, error,
                metadata_json, progress_log_json
            ) VALUES (
                :id, :user_query, :depth, :max_sources, :max_rounds,
                :status, :created_at, :updated_at,
                :progress_percent, :current_step,
                :plan_json, :generated_queries_json, :sources_json,
                :evidence_json, :claims_json, :trace_json, :report, :error,
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
                claims_json = excluded.claims_json,
                trace_json = excluded.trace_json,
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
                "evidence_json": json.dumps(job_dict.get("evidence", [])),
                "claims_json": json.dumps(job_dict.get("claims", [])),
                "trace_json": json.dumps(job_dict.get("trace")) if job_dict.get("trace") else None,
                "report": job_dict.get("report", ""),
                "error": job_dict.get("error"),
                "metadata_json": json.dumps(metadata),
                "progress_log_json": json.dumps(job_dict.get("progress_log", [])),
            },
        )
        # Only the initial trusted creation can attach query provenance. Ordinary
        # job updates cannot upgrade it, including by copying client metadata.
        if query_label is not None and is_new:
            conn.execute(
                "UPDATE research_jobs SET query_label_json = ?, query_hash = ? "
                "WHERE id = ? AND query_label_json IS NULL AND query_hash IS NULL "
                "AND created_at = ?",
                (
                    to_json(label_of(query_label)),
                    content_hash(job_dict["user_query"]),
                    job_id,
                    job_dict.get("created_at", now),
                ),
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
    """History rows without the heavy JSON payloads."""
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT id, user_query, depth, status, progress_percent, current_step, created_at,
                   length(report) > 0 AS has_report
            FROM research_jobs ORDER BY created_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_job_status(job_id: str, status: str, **kwargs) -> None:
    conn = _connect()
    try:
        now = datetime.now(UTC).isoformat()
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, now]
        for key, val in kwargs.items():
            if key in ("progress_percent", "current_step", "report", "error"):
                sets.append(f"{key} = ?")
                vals.append(val)
            elif key in _JSON_COLUMNS:
                sets.append(f"{key} = ?")
                if key == "metadata_json":
                    val = _server_metadata(conn, job_id, val)
                vals.append(json.dumps(val) if val is not None else None)
        vals.append(job_id)
        conn.execute(f"UPDATE research_jobs SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    finally:
        conn.close()


_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
#: A queued job this recent may be about to start in this process, so it is left alone.
_QUEUED_GRACE_SECONDS = 60


def mark_interrupted_jobs(active_ids: set[str]) -> int:
    """Fail jobs left mid-run by a process that is no longer running them.

    Research runs on in-process threads, so a restart leaves rows in a running state that
    nothing will ever finish; without this the UI polls them forever. ``active_ids`` are the
    jobs this process is running right now, which are never touched.
    """
    conn = _connect()
    try:
        now = datetime.now(UTC)
        rows = conn.execute(
            "SELECT id, status, updated_at FROM research_jobs WHERE status NOT IN (?, ?, ?)",
            _TERMINAL_STATUSES,
        ).fetchall()
        stale = []
        for row in rows:
            if row["id"] in active_ids:
                continue
            if row["status"] == "queued":
                try:
                    updated = datetime.fromisoformat(row["updated_at"])
                except (TypeError, ValueError):
                    updated = None
                if updated is not None and (now - updated).total_seconds() < _QUEUED_GRACE_SECONDS:
                    continue
            stale.append(row["id"])
        for job_id in stale:
            conn.execute(
                "UPDATE research_jobs SET status = 'failed', error = ?, current_step = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    "Research was interrupted because Neo restarted before it finished.",
                    "Research interrupted",
                    now.isoformat(),
                    job_id,
                ),
            )
        conn.commit()
        return len(stale)
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
    data = dict(row)
    for column, field in _JSON_COLUMNS.items():
        raw = data.pop(column, None)
        if raw:
            data[field] = json.loads(raw)
        elif field in _LIST_FIELDS:
            data[field] = []
        elif field in _NULLABLE_FIELDS:
            data[field] = None
        else:
            data[field] = {}
    return data


def query_label_for(job: dict) -> Label:
    """A saved query is reusable only with its original server-stored binding."""
    if job.get("query_hash") != content_hash(job.get("user_query")):
        return Label.unknown("research_query_changed_or_legacy")
    return from_json(job.get("query_label_json"))
