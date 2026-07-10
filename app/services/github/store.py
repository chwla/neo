from __future__ import annotations

# ruff: noqa: E501
import json
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings
from app.services.github.redaction import redact


def _connect():
    url = get_settings().database_url
    c = sqlite3.connect(
        url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db",
        timeout=30,
    )
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def now():
    return datetime.now(UTC).isoformat()


def initialize_github_tables():
    c = _connect()
    try:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_github_connections (id TEXT PRIMARY KEY,name TEXT NOT NULL,owner TEXT NOT NULL,repo TEXT NOT NULL,token_ref TEXT NOT NULL DEFAULT 'GITHUB_TOKEN',enabled INTEGER NOT NULL DEFAULT 1,metadata_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workspace_github_items (id TEXT PRIMARY KEY,connection_id TEXT NOT NULL,item_type TEXT NOT NULL,github_number INTEGER NOT NULL,github_id TEXT,title TEXT NOT NULL,state TEXT,author TEXT,body_text TEXT,labels_json TEXT,url TEXT,imported_task_id TEXT,imported_project_id TEXT,metadata_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(connection_id) REFERENCES workspace_github_connections(id));
        CREATE TABLE IF NOT EXISTS workspace_github_operations (id TEXT PRIMARY KEY,connection_id TEXT NOT NULL,item_id TEXT,operation_type TEXT NOT NULL,status TEXT NOT NULL,approval_required INTEGER NOT NULL DEFAULT 1,request_json TEXT,response_json TEXT,error TEXT,created_at TEXT NOT NULL,completed_at TEXT,FOREIGN KEY(connection_id) REFERENCES workspace_github_connections(id),FOREIGN KEY(item_id) REFERENCES workspace_github_items(id));
        CREATE INDEX IF NOT EXISTS idx_github_items_connection ON workspace_github_items(connection_id,item_type,github_number);
        CREATE INDEX IF NOT EXISTS idx_github_items_task ON workspace_github_items(imported_task_id);
        CREATE INDEX IF NOT EXISTS idx_github_operations_status ON workspace_github_operations(connection_id,status,created_at);
        """)
        c.commit()
    finally:
        c.close()


def _row(row):
    if not row:
        return None
    x = dict(row)
    for key in ("metadata_json", "labels_json", "request_json", "response_json"):
        if key in x:
            x[key.removesuffix("_json")] = json.loads(
                x.pop(key) or ("[]" if key == "labels_json" else "{}")
            )
    if "enabled" in x:
        x["enabled"] = bool(x["enabled"])
    if "approval_required" in x:
        x["approval_required"] = bool(x["approval_required"])
    return redact(x)


def list_rows(table, where="1=1", params=()):
    c = _connect()
    try:
        return [
            _row(r)
            for r in c.execute(
                f"SELECT * FROM {table} WHERE {where} ORDER BY created_at DESC", params
            )
        ]
    finally:
        c.close()


def get_row(table, id):
    c = _connect()
    try:
        return _row(c.execute(f"SELECT * FROM {table} WHERE id=?", (id,)).fetchone())
    finally:
        c.close()


def save_connection(data, id=None):
    c = _connect()
    timestamp = now()
    id = id or str(uuid.uuid4())
    try:
        if id and get_row("workspace_github_connections", id):
            keys = [
                k
                for k in ("name", "owner", "repo", "token_ref", "enabled")
                if k in data and data[k] is not None
            ]
            sets = ", ".join(f"{k}=?" for k in keys) + ", updated_at=?"
            c.execute(
                f"UPDATE workspace_github_connections SET {sets} WHERE id=?",
                [*[int(data[k]) if k == "enabled" else data[k] for k in keys], timestamp, id],
            )
        else:
            c.execute(
                "INSERT INTO workspace_github_connections VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    data["name"],
                    data["owner"],
                    data["repo"],
                    data.get("token_ref", "GITHUB_TOKEN"),
                    int(data.get("enabled", True)),
                    "{}",
                    timestamp,
                    timestamp,
                ),
            )
        c.commit()
        return get_row("workspace_github_connections", id)
    finally:
        c.close()


def disable_connection(id):
    return save_connection({"enabled": False}, id)


def save_item(data):
    c = _connect()
    timestamp = now()
    existing = c.execute(
        "SELECT id FROM workspace_github_items WHERE connection_id=? AND item_type=? AND github_number=?",
        (data["connection_id"], data["item_type"], data["github_number"]),
    ).fetchone()
    id = existing[0] if existing else str(uuid.uuid4())
    values = (
        data["connection_id"],
        data["item_type"],
        data["github_number"],
        data.get("github_id"),
        data["title"],
        data.get("state"),
        data.get("author"),
        data.get("body_text", ""),
        json.dumps(data.get("labels", [])),
        data.get("url"),
        json.dumps(data.get("metadata", {})),
        timestamp,
        id,
    )
    try:
        if existing:
            c.execute(
                "UPDATE workspace_github_items SET connection_id=?,item_type=?,github_number=?,github_id=?,title=?,state=?,author=?,body_text=?,labels_json=?,url=?,metadata_json=?,updated_at=? WHERE id=?",
                values,
            )
        else:
            c.execute(
                "INSERT INTO workspace_github_items (id,connection_id,item_type,github_number,github_id,title,state,author,body_text,labels_json,url,metadata_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (id, *values[:-1], timestamp),
            )
        c.commit()
        return get_row("workspace_github_items", id)
    finally:
        c.close()


def update_item(id, **data):
    c = _connect()
    try:
        c.execute(
            "UPDATE workspace_github_items SET imported_task_id=?, imported_project_id=?, updated_at=? WHERE id=?",
            (data.get("imported_task_id"), data.get("imported_project_id"), now(), id),
        )
        c.commit()
        return get_row("workspace_github_items", id)
    finally:
        c.close()


def operation(connection_id, item_id, typ, status, request=None, response=None, error=None):
    c = _connect()
    id = str(uuid.uuid4())
    stamp = now()
    try:
        c.execute(
            "INSERT INTO workspace_github_operations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                id,
                connection_id,
                item_id,
                typ,
                status,
                1,
                json.dumps(redact(request or {})),
                json.dumps(redact(response or {})),
                error,
                stamp,
                stamp,
            ),
        )
        c.commit()
        return get_row("workspace_github_operations", id)
    finally:
        c.close()
