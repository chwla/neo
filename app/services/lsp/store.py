from __future__ import annotations

# ruff: noqa: E501
import sqlite3
import uuid
from datetime import UTC, datetime

from app.core.config import get_settings


def _c():
    u = get_settings().database_url
    c = sqlite3.connect(
        u.replace("sqlite:///", "", 1) if u.startswith("sqlite:///") else "neo_memory.db"
    )
    c.row_factory = sqlite3.Row
    return c


def now():
    return datetime.now(UTC).isoformat()


def initialize_lsp_tables():
    c = _c()
    c.executescript(
        """CREATE TABLE IF NOT EXISTS workspace_lsp_sessions (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,language TEXT NOT NULL,server_command TEXT NOT NULL,status TEXT NOT NULL,capabilities_json TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);CREATE TABLE IF NOT EXISTS workspace_lsp_diagnostics (id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,file_path TEXT NOT NULL,language TEXT NOT NULL,severity TEXT,message TEXT NOT NULL,source TEXT,range_json TEXT,metadata_json TEXT,created_at TEXT NOT NULL);CREATE INDEX IF NOT EXISTS idx_lsp_session_workspace ON workspace_lsp_sessions(workspace_id,language,status);CREATE INDEX IF NOT EXISTS idx_lsp_diag_workspace ON workspace_lsp_diagnostics(workspace_id,file_path);"""
    )
    c.commit()
    c.close()


def sessions(workspace_id=None):
    c = _c()
    q = (
        "SELECT * FROM workspace_lsp_sessions"
        + (" WHERE workspace_id=?" if workspace_id else "")
        + " ORDER BY updated_at DESC"
    )
    r = [dict(x) for x in c.execute(q, (workspace_id,) if workspace_id else ())]
    c.close()
    return r


def save_session(workspace_id, language, command, status, error=None):
    c = _c()
    t = now()
    row = c.execute(
        "SELECT id FROM workspace_lsp_sessions WHERE workspace_id=? AND language=?",
        (workspace_id, language),
    ).fetchone()
    id = row[0] if row else str(uuid.uuid4())
    if row:
        c.execute(
            "UPDATE workspace_lsp_sessions SET server_command=?,status=?,error=?,updated_at=? WHERE id=?",
            (command, status, error, t, id),
        )
    else:
        c.execute(
            "INSERT INTO workspace_lsp_sessions VALUES (?,?,?,?,?,?,?,?,?)",
            (id, workspace_id, language, command, status, "{}", error, t, t),
        )
    c.commit()
    c.close()
    return id
