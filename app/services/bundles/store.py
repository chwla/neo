from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import get_settings


def _path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def now() -> str:
    return datetime.now(UTC).isoformat()


def bundle_dir() -> Path:
    root = Path(get_settings().data_dir or "data") / "bundles"
    root.mkdir(parents=True, exist_ok=True)
    return root


def initialize_bundle_tables() -> None:
    conn = connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_export_bundles (
          id TEXT PRIMARY KEY, bundle_type TEXT NOT NULL, root_entity_id TEXT NOT NULL,
          file_name TEXT NOT NULL, file_sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
          status TEXT NOT NULL, metadata_json TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_import_bundles (
          id TEXT PRIMARY KEY, file_name TEXT NOT NULL, file_sha256 TEXT NOT NULL,
          size_bytes INTEGER NOT NULL, status TEXT NOT NULL, imported_entity_ids_json TEXT,
          warnings_json TEXT, metadata_json TEXT, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_workspace_export_bundles_lookup
          ON workspace_export_bundles(bundle_type, root_entity_id, status, created_at);
        CREATE INDEX IF NOT EXISTS idx_workspace_import_bundles_lookup
          ON workspace_import_bundles(status, created_at);
        """)
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    for key in ("metadata_json", "warnings_json", "imported_entity_ids_json"):
        if key in item:
            item[key.removesuffix("_json")] = json.loads(
                item.pop(key) or ("[]" if key != "metadata_json" else "{}")
            )
    return item


def record_export(
    *, bundle_type: str, root_entity_id: str, file_name: str, sha256: str, size: int, metadata: dict
) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "bundle_type": bundle_type,
        "root_entity_id": root_entity_id,
        "file_name": file_name,
        "file_sha256": sha256,
        "size_bytes": size,
        "status": "ready",
        "metadata_json": json.dumps(metadata),
        "created_at": now(),
    }
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO workspace_export_bundles VALUES
          (:id,:bundle_type,:root_entity_id,:file_name,:file_sha256,:size_bytes,:status,:metadata_json,:created_at)""",
            item,
        )
        conn.commit()
    finally:
        conn.close()
    return _row_from_item(item)


def record_import(
    *,
    file_name: str,
    sha256: str,
    size: int,
    warnings: list[str],
    metadata: dict,
    entity_ids: list[str],
) -> dict:
    item = {
        "id": str(uuid.uuid4()),
        "file_name": file_name,
        "file_sha256": sha256,
        "size_bytes": size,
        "status": "archived",
        "imported_entity_ids_json": json.dumps(entity_ids),
        "warnings_json": json.dumps(warnings),
        "metadata_json": json.dumps(metadata),
        "created_at": now(),
    }
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO workspace_import_bundles VALUES
          (:id,:file_name,:file_sha256,:size_bytes,:status,:imported_entity_ids_json,:warnings_json,:metadata_json,:created_at)""",
            item,
        )
        conn.commit()
    finally:
        conn.close()
    return _row_from_item(item)


def _row_from_item(item: dict) -> dict:
    return (
        _row(sqlite3.Row)
        if False
        else {
            **{k: v for k, v in item.items() if not k.endswith("_json")},
            **{
                k.removesuffix("_json"): json.loads(v)
                for k, v in item.items()
                if k.endswith("_json")
            },
        }
    )


def list_exports() -> list[dict]:
    return _list("workspace_export_bundles")


def list_imports() -> list[dict]:
    return _list("workspace_import_bundles")


def get_export(item_id: str) -> dict | None:
    return _get("workspace_export_bundles", item_id)


def get_import(item_id: str) -> dict | None:
    return _get("workspace_import_bundles", item_id)


def _list(table: str) -> list[dict]:
    conn = connect()
    try:
        return [
            _row(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY created_at DESC")
        ]
    finally:
        conn.close()


def _get(table: str, item_id: str) -> dict | None:
    conn = connect()
    try:
        return _row(conn.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone())
    finally:
        conn.close()
