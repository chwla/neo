"""SQLite persistence for the gallery.

Follows the shape of ``app/services/files/store.py``: raw sqlite3 against the
active profile database, idempotent ``CREATE TABLE IF NOT EXISTS`` at startup,
and one module-level function per operation. The profile contextvar is read
through ``get_settings()`` on every connect, so a gallery is per-account for free.

The bytes themselves are never stored here. A gallery item points at a
``workspace_files`` row, which already owns the file on disk, its sha256 and its
soft-delete flag -- so re-uploading the same photo is deduplicated before this
module ever sees it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.services.gallery.images import hamming_distance

#: Ranking reads every candidate row, so it is fetched in one pass rather than
#: per item. Well beyond a realistic personal gallery, and bounded so a runaway
#: import cannot turn search into a full-table scan of unbounded size.
SEARCH_SCAN_LIMIT = 5000


def _db_path() -> str:
    url = get_settings().database_url
    return url.replace("sqlite:///", "", 1) if url.startswith("sqlite:///") else "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


_FTS_TABLE = "gallery_items_fts"


def _fts_available(conn: sqlite3.Connection) -> bool:
    """Whether this SQLite build has fts5.

    A stripped build must degrade to LIKE rather than refuse to start, which is
    the rule the memory indexes already follow.
    """

    try:
        conn.execute(f"SELECT 1 FROM {_FTS_TABLE} LIMIT 1")
        return True
    except sqlite3.OperationalError:
        return False


def initialize_gallery_tables() -> None:
    conn = _connect()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS gallery_items (
                id TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                title TEXT,
                caption TEXT,
                ocr_text TEXT,
                alt_text TEXT,
                tags_json TEXT,
                width INTEGER, height INTEGER, image_format TEXT,
                thumbnail_path TEXT,
                phash TEXT,
                origin TEXT NOT NULL DEFAULT 'upload',
                description_status TEXT NOT NULL DEFAULT 'pending',
                description_model TEXT, description_error TEXT, described_at TEXT,
                user_edited INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY (file_id) REFERENCES workspace_files(id)
            );
            CREATE TABLE IF NOT EXISTS gallery_appearances (
                id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                chat_id INTEGER, message_id INTEGER,
                agent_session_id TEXT, project_id TEXT,
                role TEXT,
                seen_at TEXT NOT NULL,
                FOREIGN KEY (item_id) REFERENCES gallery_items(id),
                UNIQUE(item_id, chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS gallery_preferences (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gallery_vectors (
                item_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL, model TEXT NOT NULL, provider_version TEXT NOT NULL,
                dimension INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (item_id) REFERENCES gallery_items(id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gallery_items_file
                ON gallery_items(file_id);
            CREATE INDEX IF NOT EXISTS idx_gallery_items_live
                ON gallery_items(deleted, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_gallery_items_status
                ON gallery_items(description_status);
            CREATE INDEX IF NOT EXISTS idx_gallery_items_phash ON gallery_items(phash);
            -- The table-level UNIQUE cannot carry this on its own: SQL treats
            -- two NULLs as distinct, so an appearance with no message_id would
            -- insert again on every replay and let one image inflate its own
            -- recency. IFNULL collapses them, the way the memory schema's
            -- COALESCE does for a nullable scope.
            CREATE UNIQUE INDEX IF NOT EXISTS idx_gallery_appearances_unique
                ON gallery_appearances(item_id, IFNULL(chat_id, -1), IFNULL(message_id, -1));
            CREATE INDEX IF NOT EXISTS idx_gallery_appearances_chat
                ON gallery_appearances(chat_id, seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_gallery_appearances_item
                ON gallery_appearances(item_id, seen_at DESC);
        """)
        try:
            conn.executescript(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} "
                "USING fts5(item_id UNINDEXED, title, caption, ocr_text, tags_text);"
            )
        except sqlite3.OperationalError:
            # No fts5 in this build. Search falls back to LIKE.
            pass
        conn.commit()
    finally:
        conn.close()


def _item_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["tags"] = json.loads(item.pop("tags_json") or "[]")
    for flag in ("deleted", "pinned", "user_edited"):
        item[flag] = bool(item[flag])
    return item


def _appearance_row(row: sqlite3.Row) -> dict:
    return dict(row)


# --------------------------------------------------------------------------- items


def insert_item(item: dict[str, Any]) -> dict:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO gallery_items (
                id, file_id, title, caption, ocr_text, alt_text, tags_json,
                width, height, image_format, thumbnail_path, phash, origin,
                description_status, description_model, description_error, described_at,
                user_edited, pinned, deleted, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item["file_id"],
                item.get("title"),
                item.get("caption"),
                item.get("ocr_text"),
                item.get("alt_text"),
                json.dumps(item.get("tags", [])),
                item.get("width"),
                item.get("height"),
                item.get("image_format"),
                item.get("thumbnail_path"),
                item.get("phash"),
                item.get("origin", "upload"),
                item.get("description_status", "pending"),
                item.get("description_model"),
                item.get("description_error"),
                item.get("described_at"),
                int(item.get("user_edited", False)),
                int(item.get("pinned", False)),
                int(item.get("deleted", False)),
                item["created_at"],
                item["updated_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    reindex_item(item["id"])
    return get_item(item["id"], include_deleted=True) or item


#: Columns update_item will write. Anything else in a patch is ignored rather
#: than interpolated, so a caller cannot reach a column by naming one.
_UPDATABLE = {
    "title",
    "caption",
    "ocr_text",
    "alt_text",
    "width",
    "height",
    "image_format",
    "thumbnail_path",
    "phash",
    "description_status",
    "description_model",
    "description_error",
    "described_at",
    "user_edited",
    "pinned",
    "deleted",
}


def update_item(item_id: str, patch: dict[str, Any]) -> dict | None:
    fields = {key: value for key, value in patch.items() if key in _UPDATABLE}
    if "tags" in patch:
        fields["tags_json"] = json.dumps(patch["tags"] or [])
    for flag in ("user_edited", "pinned", "deleted"):
        if flag in fields:
            fields[flag] = int(bool(fields[flag]))
    if not fields:
        return get_item(item_id, include_deleted=True)
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn = _connect()
    try:
        conn.execute(
            f"UPDATE gallery_items SET {assignments} WHERE id = ?",
            (*fields.values(), item_id),
        )
        conn.commit()
    finally:
        conn.close()
    reindex_item(item_id)
    return get_item(item_id, include_deleted=True)


def get_item(item_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM gallery_items WHERE id = ?"
        if not include_deleted:
            sql += " AND deleted = 0"
        return _item_row(conn.execute(sql, (item_id,)).fetchone())
    finally:
        conn.close()


def get_preference(key: str) -> str | None:
    """The stored value for a gallery preference, or None if never set."""

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM gallery_preferences WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_preference(key: str, value: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO gallery_preferences (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, value, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def get_item_by_file(file_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        sql = "SELECT * FROM gallery_items WHERE file_id = ?"
        if not include_deleted:
            sql += " AND deleted = 0"
        return _item_row(conn.execute(sql, (file_id,)).fetchone())
    finally:
        conn.close()


def find_by_phash(phash: str, *, max_distance: int = 0) -> list[dict]:
    """Rows whose perceptual hash is within ``max_distance`` bits.

    Exact matches use the index; a non-zero distance needs the scan, which is why
    near-duplicate detection is offered rather than applied automatically.
    """

    conn = _connect()
    try:
        if max_distance <= 0:
            rows = conn.execute(
                "SELECT * FROM gallery_items WHERE phash = ? AND deleted = 0", (phash,)
            ).fetchall()
            return [item for item in map(_item_row, rows) if item]
        rows = conn.execute(
            "SELECT * FROM gallery_items WHERE deleted = 0 AND phash IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (SEARCH_SCAN_LIMIT,),
        ).fetchall()
    finally:
        conn.close()
    matches = []
    for row in rows:
        item = _item_row(row)
        if item and hamming_distance(item["phash"], phash) <= max_distance:
            matches.append(item)
    return matches


def list_items(
    *,
    q: str | None = None,
    chat_id: int | None = None,
    project_id: str | None = None,
    tags: list[str] | None = None,
    status: str | None = None,
    origin: str | None = None,
    pinned: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    include_deleted: bool = False,
    limit: int = 60,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where = ["1 = 1"]
    params: list[Any] = []
    if not include_deleted:
        where.append("i.deleted = 0")
    if q:
        where.append(
            "(IFNULL(i.title,'') LIKE ? OR IFNULL(i.caption,'') LIKE ? "
            "OR IFNULL(i.ocr_text,'') LIKE ? OR IFNULL(i.tags_json,'') LIKE ?)"
        )
        params.extend([f"%{q}%"] * 4)
    if status:
        where.append("i.description_status = ?")
        params.append(status)
    if origin:
        where.append("i.origin = ?")
        params.append(origin)
    if pinned is not None:
        where.append("i.pinned = ?")
        params.append(int(pinned))
    for tag in tags or []:
        where.append("IFNULL(i.tags_json,'') LIKE ?")
        params.append(f'%"{tag}"%')
    # Which conversation an image belongs to is a fact about its sightings, so it
    # is a strict test: an image never shown in chat 42 does not belong to it.
    scope = []
    if chat_id is not None:
        scope.append(("a.chat_id = ?", chat_id))
    if project_id:
        scope.append(("a.project_id = ?", project_id))
    if scope:
        clause = " AND ".join(condition for condition, _ in scope)
        where.append(
            f"EXISTS (SELECT 1 FROM gallery_appearances a WHERE a.item_id = i.id AND {clause})"
        )
        params.extend(value for _, value in scope)
    # "Last week" means the sighting, because that is what the user remembers --
    # but an image added straight to the gallery has no sighting at all, and
    # filtering it out would make every dated search silently blind to it. So
    # the item's own arrival stands in when it was never shown.
    period = []
    if since:
        period.append((">=", since))
    if until:
        period.append(("<=", until))
    if period:
        seen = " AND ".join(f"a.seen_at {op} ?" for op, _ in period)
        created = " AND ".join(f"i.created_at {op} ?" for op, _ in period)
        where.append(
            f"(EXISTS (SELECT 1 FROM gallery_appearances a WHERE a.item_id = i.id AND {seen})"
            " OR (NOT EXISTS (SELECT 1 FROM gallery_appearances a2 WHERE a2.item_id = i.id)"
            f" AND {created}))"
        )
        params.extend(value for _, value in period)
        params.extend(value for _, value in period)
    clause = " AND ".join(where)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM gallery_items i WHERE {clause}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM gallery_items i WHERE {clause} "
            "ORDER BY i.pinned DESC, i.created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [item for item in map(_item_row, rows) if item], int(total)
    finally:
        conn.close()


def scan_items(*, since: str | None = None, until: str | None = None) -> list[dict]:
    """Every live item, for ranking to score in memory."""

    items, _ = list_items(since=since, until=until, limit=SEARCH_SCAN_LIMIT, offset=0)
    return items


def delete_item(item_id: str) -> None:
    """Remove the row and everything derived from it. Bytes are the caller's job."""

    conn = _connect()
    try:
        conn.execute("DELETE FROM gallery_vectors WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM gallery_appearances WHERE item_id = ?", (item_id,))
        if _fts_available(conn):
            conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE item_id = ?", (item_id,))
        conn.execute("DELETE FROM gallery_items WHERE id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------- appearances


def record_appearance(entry: dict[str, Any]) -> dict | None:
    """Note that an item was seen somewhere.

    Idempotent on ``(item_id, chat_id, message_id)``: replaying a turn records the
    sighting once, so the same photo in the same message cannot inflate its own
    recency.
    """

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO gallery_appearances (
                id, item_id, chat_id, message_id, agent_session_id, project_id, role, seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["id"],
                entry["item_id"],
                entry.get("chat_id"),
                entry.get("message_id"),
                entry.get("agent_session_id"),
                entry.get("project_id"),
                entry.get("role", "user"),
                entry.get("seen_at") or now_iso(),
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM gallery_appearances WHERE id = ?", (entry["id"],)
        ).fetchone()
        return _appearance_row(row) if row else None
    finally:
        conn.close()


def list_appearances(item_id: str) -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM gallery_appearances WHERE item_id = ? ORDER BY seen_at DESC",
            (item_id,),
        ).fetchall()
        return [_appearance_row(row) for row in rows]
    finally:
        conn.close()


def appearances_for(item_ids: list[str]) -> dict[str, list[dict]]:
    """Appearances for many items at once, so ranking does not query per result."""

    if not item_ids:
        return {}
    placeholders = ", ".join("?" for _ in item_ids)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT * FROM gallery_appearances WHERE item_id IN ({placeholders}) "
            "ORDER BY seen_at DESC",
            item_ids,
        ).fetchall()
    finally:
        conn.close()
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["item_id"], []).append(_appearance_row(row))
    return grouped


# ------------------------------------------------------------------------- search


def reindex_item(item_id: str) -> None:
    """Rewrite this item's FTS row. Safe to call after any write."""

    conn = _connect()
    try:
        if not _fts_available(conn):
            return
        row = conn.execute("SELECT * FROM gallery_items WHERE id = ?", (item_id,)).fetchone()
        conn.execute(f"DELETE FROM {_FTS_TABLE} WHERE item_id = ?", (item_id,))
        if row and not row["deleted"]:
            tags = " ".join(json.loads(row["tags_json"] or "[]"))
            conn.execute(
                f"INSERT INTO {_FTS_TABLE} (item_id, title, caption, ocr_text, tags_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (row["id"], row["title"] or "", row["caption"] or "", row["ocr_text"] or "", tags),
            )
        conn.commit()
    finally:
        conn.close()


def fts_scores(query: str, limit: int = 200) -> dict[str, float]:
    """Item id -> normalised bm25 score, or empty when fts5 is unavailable.

    Normalisation matches the memory index: ``1 / (1 + |rank|)``, so a score is
    comparable with the cosine similarity it gets blended with.
    """

    terms = [term for term in _tokenize(query) if term]
    if not terms:
        return {}
    match = " OR ".join(f'"{term}"' for term in terms)
    conn = _connect()
    try:
        if not _fts_available(conn):
            return {}
        rows = conn.execute(
            f"SELECT item_id, bm25({_FTS_TABLE}) AS rank FROM {_FTS_TABLE} "
            f"WHERE {_FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {row["item_id"]: 1.0 / (1.0 + abs(float(row["rank"]))) for row in rows}


def _tokenize(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (value or "").lower())


# ------------------------------------------------------------------------ vectors


def upsert_vector(entry: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO gallery_vectors (
                item_id, provider, model, provider_version, dimension,
                content_hash, vector_blob, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                provider = excluded.provider, model = excluded.model,
                provider_version = excluded.provider_version, dimension = excluded.dimension,
                content_hash = excluded.content_hash, vector_blob = excluded.vector_blob,
                updated_at = excluded.updated_at
            """,
            (
                entry["item_id"],
                entry["provider"],
                entry["model"],
                entry["provider_version"],
                entry["dimension"],
                entry["content_hash"],
                entry["vector_blob"],
                entry.get("updated_at") or now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_vector(item_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM gallery_vectors WHERE item_id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def all_vectors() -> list[dict]:
    """Every stored vector, for the brute-force cosine pass."""

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT v.* FROM gallery_vectors v "
            "JOIN gallery_items i ON i.id = v.item_id AND i.deleted = 0 "
            "LIMIT ?",
            (SEARCH_SCAN_LIMIT,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def delete_vector(item_id: str) -> None:
    conn = _connect()
    try:
        conn.execute("DELETE FROM gallery_vectors WHERE item_id = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()
