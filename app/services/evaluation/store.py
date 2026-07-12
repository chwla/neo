from __future__ import annotations

# ruff: noqa: E501
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings

from .redaction import redact


def _now():
    return datetime.now(UTC).isoformat()


def _connect():
    url = get_settings().database_url
    path = url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_evaluation_tables():
    conn = _connect()
    try:
        conn.executescript("""CREATE TABLE IF NOT EXISTS workspace_eval_suites (id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,description TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,config_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_eval_runs (id TEXT PRIMARY KEY,suite_id TEXT NOT NULL,status TEXT NOT NULL,fixture_mode INTEGER NOT NULL,overall_score REAL,hard_failure_count INTEGER NOT NULL DEFAULT 0,summary_json TEXT,created_at TEXT NOT NULL,completed_at TEXT);
CREATE TABLE IF NOT EXISTS workspace_eval_cases (id TEXT PRIMARY KEY,suite_id TEXT NOT NULL,name TEXT NOT NULL,case_type TEXT NOT NULL,input_json TEXT NOT NULL,expected_json TEXT NOT NULL,fixture_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_eval_case_results (id TEXT PRIMARY KEY,run_id TEXT NOT NULL,case_id TEXT NOT NULL,status TEXT NOT NULL,score REAL NOT NULL,metrics_json TEXT NOT NULL,hard_failures_json TEXT NOT NULL,warnings_json TEXT NOT NULL,output_json TEXT NOT NULL,artifacts_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_eval_baselines (id TEXT PRIMARY KEY,suite_id TEXT NOT NULL,run_id TEXT NOT NULL,name TEXT NOT NULL,threshold REAL NOT NULL DEFAULT 0.05,created_at TEXT NOT NULL);""")
        conn.commit()
    finally:
        conn.close()


def _row(row):
    if not row:
        return None
    d = dict(row)
    for key in list(d):
        if key.endswith("_json"):
            d[key[:-5]] = json.loads(d.pop(key) or "{}")
    if "fixture_mode" in d:
        d["fixture_mode"] = bool(d["fixture_mode"])
    return redact(d)


def _id():
    return str(uuid.uuid4())


def suites():
    initialize_evaluation_tables()
    c = _connect()
    try:
        return [_row(r) for r in c.execute("SELECT * FROM workspace_eval_suites ORDER BY name")]
    finally:
        c.close()


def suite(sid):
    c = _connect()
    try:
        return _row(
            c.execute(
                "SELECT * FROM workspace_eval_suites WHERE id=? OR name=?", (sid, sid)
            ).fetchone()
        )
    finally:
        c.close()


def create_suite(name, description, config, cases):
    initialize_evaluation_tables()
    existing = suite(name)
    if existing:
        if globals()["cases"](existing["id"]):
            return existing
        sid = existing["id"]
    else:
        sid = _id()
    now = _now()
    c = _connect()
    try:
        if not existing:
            c.execute(
                "INSERT INTO workspace_eval_suites VALUES (?,?,?,?,?,?)",
                (sid, name, description, now, now, json.dumps(redact(config))),
            )
        for item in cases:
            c.execute(
                "INSERT INTO workspace_eval_cases VALUES (?,?,?,?,?,?,?,?)",
                (
                    _id(),
                    sid,
                    item["name"],
                    item["case_type"],
                    json.dumps(redact(item.get("input", {}))),
                    json.dumps(redact(item.get("expected", {}))),
                    json.dumps(redact(item.get("fixture", {}))),
                    now,
                ),
            )
        c.commit()
    finally:
        c.close()
    return suite(sid)


def cases(sid):
    c = _connect()
    try:
        return [
            _row(r)
            for r in c.execute("SELECT * FROM workspace_eval_cases WHERE suite_id=?", (sid,))
        ]
    finally:
        c.close()


def create_run(sid, fixture_mode):
    rid = _id()
    c = _connect()
    try:
        c.execute(
            "INSERT INTO workspace_eval_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, sid, "running", int(fixture_mode), None, 0, None, _now(), None),
        )
        c.commit()
    finally:
        c.close()
    return run(rid)


def finish_run(rid, score, hard_count, summary):
    c = _connect()
    try:
        c.execute(
            "UPDATE workspace_eval_runs SET status='completed',overall_score=?,hard_failure_count=?,summary_json=?,completed_at=? WHERE id=?",  # noqa: E501
            (score, hard_count, json.dumps(redact(summary)), _now(), rid),
        )
        c.commit()
    finally:
        c.close()
    return run(rid)


def run(rid):
    c = _connect()
    try:
        return _row(c.execute("SELECT * FROM workspace_eval_runs WHERE id=?", (rid,)).fetchone())
    finally:
        c.close()


def runs(limit=100):
    c = _connect()
    try:
        return [
            _row(r)
            for r in c.execute(
                "SELECT * FROM workspace_eval_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        ]
    finally:
        c.close()


def add_result(rid, cid, result, output):
    c = _connect()
    try:
        c.execute(
            "INSERT INTO workspace_eval_case_results VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                _id(),
                rid,
                cid,
                "failed" if result["hard_failures"] else "passed",
                result["score"],
                json.dumps(result["metrics"]),
                json.dumps(result["hard_failures"]),
                json.dumps(result["warnings"]),
                json.dumps(redact(output)),
                json.dumps({}),
                _now(),
            ),
        )
        c.commit()
    finally:
        c.close()


def results(rid):
    c = _connect()
    try:
        return [
            _row(r)
            for r in c.execute(
                "SELECT r.*,c.name,c.case_type FROM workspace_eval_case_results r JOIN workspace_eval_cases c ON c.id=r.case_id WHERE r.run_id=?",  # noqa: E501
                (rid,),
            )
        ]
    finally:
        c.close()


def baseline(rid, name, threshold=0.05):
    value = run(rid)
    if not value:
        raise LookupError("Evaluation run not found.")
    bid = _id()
    c = _connect()
    try:
        c.execute(
            "INSERT INTO workspace_eval_baselines VALUES (?,?,?,?,?,?)",
            (bid, value["suite_id"], rid, name, threshold, _now()),
        )
        c.commit()
    finally:
        c.close()
    c = _connect()
    try:
        return _row(
            c.execute("SELECT * FROM workspace_eval_baselines WHERE id=?", (bid,)).fetchone()
        )
    finally:
        c.close()


def baselines():
    c = _connect()
    try:
        return [
            _row(r) for r in c.execute("SELECT * FROM workspace_eval_baselines ORDER BY created_at")
        ]
    finally:
        c.close()


def delete_run(rid):
    c = _connect()
    try:
        c.execute("DELETE FROM workspace_eval_case_results WHERE run_id=?", (rid,))
        c.execute("DELETE FROM workspace_eval_runs WHERE id=?", (rid,))
        c.commit()
    finally:
        c.close()
