# ruff: noqa
from __future__ import annotations

# ruff: noqa: E501
import json, sqlite3, uuid
from datetime import UTC, datetime
from app.core.config import get_settings
from .redaction import redact


def now():
    return datetime.now(UTC).isoformat()


def uid():
    return str(uuid.uuid4())


def conn():
    url = get_settings().database_url
    c = sqlite3.connect(
        url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db",
        timeout=30,
    )
    c.row_factory = sqlite3.Row
    return c


def initialize_workspace_orchestration_tables():
    c = conn()
    try:
        c.executescript("""CREATE TABLE IF NOT EXISTS workspace_orchestration_workspaces (id TEXT PRIMARY KEY,name TEXT NOT NULL,goal TEXT NOT NULL,scope_text TEXT,status TEXT NOT NULL,readiness_status TEXT NOT NULL,health_score REAL,constraints_json TEXT,metadata_json TEXT,created_by TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,archived_at TEXT);
CREATE TABLE IF NOT EXISTS workspace_orchestration_nodes (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,node_type TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,priority TEXT,linked_entity_type TEXT,linked_entity_id TEXT,metadata_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_orchestration_edges (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,from_node_id TEXT NOT NULL,to_node_id TEXT NOT NULL,edge_type TEXT NOT NULL,metadata_json TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_orchestration_events (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,event_type TEXT NOT NULL,title TEXT NOT NULL,summary TEXT,linked_entity_type TEXT,linked_entity_id TEXT,severity TEXT,metadata_json TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_orchestration_artifacts (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,artifact_type TEXT NOT NULL,title TEXT NOT NULL,linked_entity_type TEXT,linked_entity_id TEXT,content_summary TEXT,metadata_json TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workspace_orchestration_readiness_checks (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,check_key TEXT NOT NULL,check_name TEXT NOT NULL,status TEXT NOT NULL,severity TEXT,evidence_json TEXT,recommendation TEXT,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_workspace_nodes_workspace ON workspace_orchestration_nodes(workspace_id,node_type,status);CREATE INDEX IF NOT EXISTS idx_workspace_events_workspace ON workspace_orchestration_events(workspace_id,created_at);CREATE INDEX IF NOT EXISTS idx_workspace_checks_workspace ON workspace_orchestration_readiness_checks(workspace_id,check_key);""")
        c.commit()
    finally:
        c.close()


def row(r):
    if not r:
        return None
    d = dict(r)
    for k in list(d):
        if k.endswith("_json"):
            d[k[:-5]] = json.loads(d.pop(k) or "{}")
    return redact(d)


def insert(table, payload):
    c = conn()
    try:
        c.execute(
            f"INSERT INTO {table} ({','.join(payload)}) VALUES ({','.join('?' for _ in payload)})",
            list(payload.values()),
        )
        c.commit()
    finally:
        c.close()


def many(table, wid):
    c = conn()
    order = "updated_at" if table == "workspace_orchestration_readiness_checks" else "created_at"
    try:
        return [
            row(x)
            for x in c.execute(
                f"SELECT * FROM {table} WHERE workspace_id=? ORDER BY {order}", (wid,)
            )
        ]
    finally:
        c.close()
