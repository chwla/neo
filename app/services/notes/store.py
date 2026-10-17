"""SQLite persistence for notes.

Every multi-statement change runs in one explicit ``BEGIN IMMEDIATE`` transaction
(see ``_tx``), which takes SQLite's write lock before reading, so a check and the
write that depends on it can never be split by another writer.

Two kinds of write, kept apart on purpose:

* Content (title, body, document, tags, summary, ciphertext) changes only by
  compare-and-swap on ``revision`` *and* ``deleted = 0``. A write that loses the
  race, or that arrives for a note in Recently Deleted, is stored in
  ``note_conflicts`` inside the same transaction before the caller is told, so it
  survives whatever happens to the client next. Each accepted write leaves a
  receipt keyed by its ``write_id``, so a replay of a write that already landed
  is recognised instead of reported as a conflict.
* Metadata (pinned, archived, folder, trash) changes field by field and never
  touches ``revision`` or ``updated_at``: pinning or moving a note cannot conflict
  with typing in it, and does not make it "recently edited".

Connections run with ``secure_delete`` so content that is overwritten or deleted
is zeroed in the database file rather than left in free pages.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.notes import migrations
from app.services.notes.content import Derived, derive_from_doc, derive_from_markdown, snippet
from app.services.notes.smart import SmartRules, compile_rules

logger = logging.getLogger(__name__)

TRASH_RETENTION = timedelta(days=30)
RECEIPT_RETENTION = timedelta(days=30)
GC_GRACE = timedelta(hours=24)
MAINTENANCE_INTERVAL = timedelta(hours=24)
NOTE_FILE_SOURCES = ("note", "note_encrypted")
_BOOL_COLUMNS = (
    "pinned",
    "archived",
    "deleted",
    "has_images",
    "has_files",
    "has_sketches",
    "has_tables",
    "has_links",
)
_DERIVED_COLUMNS = (
    "checklist_total",
    "checklist_done",
    "has_images",
    "has_files",
    "has_sketches",
    "has_tables",
    "has_links",
    "cover_file_id",
    "preview",
    "excerpt",
)


# ------------------------------------------------------------------ errors --


class NoteNotFound(LookupError):
    pass


class NoteGone(LookupError):
    """The note was permanently deleted; nothing can be written to it."""


class NoteLocked(RuntimeError):
    """Plaintext was offered for an encrypted note. It is refused, never stored."""


class NotInTrash(RuntimeError):
    pass


class FolderNotFound(LookupError):
    pass


class SmartFolderNotFound(LookupError):
    pass


class InvalidFolderMove(ValueError):
    pass


class StaleRevision(RuntimeError):
    """An action (not a content write) was asked for on top of an old revision."""


class NoteDeleted(RuntimeError):
    """The note is in Recently Deleted; restore it before changing its content."""


class ConflictCaptured(RuntimeError):
    """A content write was not applied; it was kept in ``note_conflicts`` instead."""

    def __init__(self, conflict_id: str, reason: str, note_id: str) -> None:
        super().__init__(reason)
        self.conflict_id = conflict_id
        self.reason = reason
        self.note_id = note_id


# -------------------------------------------------------------- connection --


def _db_path() -> str:
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "", 1)
    return "neo_memory.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return migrations.table_exists(conn, name)


def migration_failure() -> str | None:
    return migrations.failure_for(_db_path())


# ---------------------------------------------------------- initialization --


def initialize_notes_tables() -> None:
    """Bring this database's notes schema up to date, then do overdue housekeeping."""

    if not migrations.run_safely(_db_path()):
        return
    try:
        run_maintenance()
    except Exception:  # noqa: BLE001 - housekeeping must never block startup
        logger.exception("Notes maintenance failed")


# ------------------------------------------------------------------- reads --


def _note_columns(include_content: bool | str) -> str:
    """Every list column, plus the content: True (all of it), "body", or False (none)."""

    if include_content == "body":
        content = "n.body"
    elif include_content:
        content = "n.body, n.doc_json, n.cipher_json"
    else:
        content = "'' AS body"
    return f"""
        n.id, n.title, n.summary, n.source_type, n.source_id, n.source_url, n.source_title,
        n.source_metadata_json, n.pinned, n.archived, n.deleted, n.created_at, n.updated_at,
        n.content_format, n.lock_key_version, n.revision, n.meta_revision, n.meta_updated_at,
        n.folder_id, n.deleted_at, n.checklist_total, n.checklist_done, n.has_images,
        n.has_files, n.has_sketches, n.has_tables, n.has_links, n.cover_file_id, n.preview,
        n.excerpt, {content},
        (SELECT group_concat(t.tag, char(31)) FROM note_tags t WHERE t.note_id = n.id)
            AS tags_concat,
        (SELECT COUNT(*) FROM note_conflicts c WHERE c.note_id = n.id AND c.resolved_at IS NULL)
            AS open_conflicts
    """


def _row_to_note(row: sqlite3.Row) -> dict:
    data = dict(row)
    raw_metadata = data.pop("source_metadata_json", None)
    data["source_metadata"] = json.loads(raw_metadata) if raw_metadata else {}
    for key in _BOOL_COLUMNS:
        if key in data:
            data[key] = bool(data[key])
    doc_json = data.pop("doc_json", None)
    data["doc"] = json.loads(doc_json) if doc_json else None
    cipher_json = data.pop("cipher_json", None)
    data["cipher"] = json.loads(cipher_json) if cipher_json else None
    tags = data.pop("tags_concat", None)
    data["tags"] = sorted(tags.split("\x1f")) if tags else []
    data["open_conflicts"] = int(data.get("open_conflicts") or 0)
    data["body"] = data.get("body") or ""
    return data


def _fetch_note(
    conn: sqlite3.Connection, note_id: str, *, include_deleted: bool = True
) -> dict | None:
    sql = f"SELECT {_note_columns(True)} FROM notes n WHERE n.id = ?"
    if not include_deleted:
        sql += " AND n.deleted = 0"
    row = conn.execute(sql, (note_id,)).fetchone()
    return _row_to_note(row) if row else None


def get_note(note_id: str, *, include_deleted: bool = False) -> dict | None:
    conn = _connect()
    try:
        return _fetch_note(conn, note_id, include_deleted=include_deleted)
    finally:
        conn.close()


def find_note_by_source(source_type: str, source_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            f"""
            SELECT {_note_columns(True)} FROM notes n
            WHERE n.source_type = ? AND n.source_id = ? AND n.deleted = 0
            ORDER BY n.updated_at DESC
            LIMIT 1
            """,
            (source_type, source_id),
        ).fetchone()
        return _row_to_note(row) if row else None
    finally:
        conn.close()


_SORTS = {"edited": "n.updated_at", "created": "n.created_at", "title": "lower(n.title)"}


def list_notes(
    *,
    q: str | None = None,
    tag: str | None = None,
    tags: list[str] | None = None,
    tags_mode: str = "any",
    include_archived: bool = False,
    pinned_first: bool = True,
    view: str | None = None,
    folder_id: str | None = None,
    smart_id: str | None = None,
    tz_offset: int = 0,
    sort: str = "edited",
    order: str = "desc",
    include_body: bool = True,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    conn = _connect()
    try:
        where: list[str] = []
        params: list[Any] = []
        if view == "smart":
            smart_sql, smart_params = _smart_where(conn, smart_id, tz_offset)
            where.append(smart_sql)
            params.extend(smart_params)
        if view == "deleted":
            where.append("n.deleted = 1 AND n.deleted_at IS NOT NULL")
        else:
            where.append("n.deleted = 0")
            if view == "archived":
                where.append("n.archived = 1")
            elif view in ("folder", "tag", "smart") or not include_archived:
                where.append("n.archived = 0")
            if view == "folder":
                if folder_id in (None, "", "default"):
                    where.append("n.folder_id IS NULL")
                else:
                    where.append("n.folder_id = ?")
                    params.append(folder_id)
        wanted = [*(tags or []), *([tag] if tag else [])]
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            if tags_mode == "all" and len(wanted) > 1:
                where.append(
                    f"(SELECT COUNT(DISTINCT ft.tag) FROM note_tags ft WHERE ft.note_id = n.id "
                    f"AND ft.tag IN ({placeholders})) = ?"
                )
                params.extend([*wanted, len(set(wanted))])
            else:
                where.append(
                    f"EXISTS (SELECT 1 FROM note_tags ft WHERE ft.note_id = n.id "
                    f"AND ft.tag IN ({placeholders}))"
                )
                params.extend(wanted)
        if q:
            like = f"%{q.lower()}%"
            where.append(
                """
                (
                    lower(n.title) LIKE ?
                    OR lower(n.body) LIKE ?
                    OR lower(coalesce(n.summary, '')) LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM note_tags st
                        WHERE st.note_id = n.id AND lower(st.tag) LIKE ?
                    )
                )
                """
            )
            params.extend([like, like, like, like])
        where_sql = " AND ".join(where)
        column = _SORTS.get(sort, "n.updated_at")
        direction = "ASC" if order == "asc" else "DESC"
        order_sql = f"{column} {direction}, n.id {direction}"
        if pinned_first and view != "deleted":
            order_sql = f"n.pinned DESC, {order_sql}"
        total = conn.execute(f"SELECT COUNT(*) FROM notes n WHERE {where_sql}", params).fetchone()[
            0
        ]
        rows = conn.execute(
            f"""
            SELECT {_note_columns(True if include_body else ("body" if q else False))} FROM notes n
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        notes = []
        for row in rows:
            note = _row_to_note(row)
            if q:
                note["snippet"] = snippet(note["body"], q)
            if not include_body:
                note["body"] = ""
                note["doc"] = None
                note["cipher"] = None
            notes.append(note)
        return notes, int(total)
    finally:
        conn.close()


def list_tags(*, include_archived: bool = True) -> list[dict]:
    conn = _connect()
    try:
        archived_sql = "" if include_archived else "AND n.archived = 0"
        rows = conn.execute(
            f"""
            SELECT nt.tag, COUNT(*) AS count
            FROM note_tags nt
            JOIN notes n ON n.id = nt.note_id
            WHERE n.deleted = 0 {archived_sql}
            GROUP BY nt.tag
            ORDER BY count DESC, nt.tag ASC
            """
        ).fetchall()
        return [{"tag": row["tag"], "count": int(row["count"])} for row in rows]
    finally:
        conn.close()


def sidebar_counts() -> dict[str, Any]:
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN deleted = 0 AND archived = 0 THEN 1 ELSE 0 END) AS all_notes,
                SUM(CASE WHEN deleted = 0 AND archived = 0 AND folder_id IS NULL THEN 1 ELSE 0 END)
                    AS default_folder,
                SUM(CASE WHEN deleted = 0 AND archived = 1 THEN 1 ELSE 0 END) AS archived,
                SUM(CASE WHEN deleted = 1 AND deleted_at IS NOT NULL THEN 1 ELSE 0 END)
                    AS recently_deleted
            FROM notes
            """
        ).fetchone()
        per_folder = {
            r["folder_id"]: int(r["count"])
            for r in conn.execute(
                "SELECT folder_id, COUNT(*) AS count FROM notes "
                "WHERE deleted = 0 AND archived = 0 AND folder_id IS NOT NULL GROUP BY folder_id"
            )
        }
        return {
            "counts": {
                "all": int(row["all_notes"] or 0),
                "default": int(row["default_folder"] or 0),
                "archived": int(row["archived"] or 0),
                "deleted": int(row["recently_deleted"] or 0),
            },
            "per_folder": per_folder,
        }
    finally:
        conn.close()


# ----------------------------------------------------------- smart folders --


def _smart_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["rules"] = json.loads(data.pop("rules_json"))
    return data


def _smart_where(
    conn: sqlite3.Connection, smart_id: str | None, tz_offset: int
) -> tuple[str, list[Any]]:
    row = conn.execute(
        "SELECT rules_json FROM note_smart_folders WHERE id = ?", (smart_id,)
    ).fetchone()
    if row is None:
        raise SmartFolderNotFound(smart_id)
    return compile_rules(
        SmartRules.model_validate_json(row["rules_json"]), tz_offset_minutes=tz_offset
    )


def list_smart_folders(*, tz_offset: int = 0) -> list[dict]:
    """Every smart folder, with how many notes it gathers now."""

    conn = _connect()
    try:
        folders = []
        for row in conn.execute("SELECT * FROM note_smart_folders ORDER BY position, name"):
            folder = _smart_row(row)
            sql, params = compile_rules(
                SmartRules.model_validate(folder["rules"]), tz_offset_minutes=tz_offset
            )
            folder["count"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM notes n "
                    f"WHERE n.deleted = 0 AND n.archived = 0 AND {sql}",
                    params,
                ).fetchone()[0]
            )
            folders.append(folder)
        return folders
    finally:
        conn.close()


def get_smart_folder(smart_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM note_smart_folders WHERE id = ?", (smart_id,)).fetchone()
        return _smart_row(row) if row else None
    finally:
        conn.close()


def create_smart_folder(name: str, rules: SmartRules) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM note_smart_folders"
            ).fetchone()[0]
            smart_id = new_id()
            now = now_iso()
            conn.execute(
                "INSERT INTO note_smart_folders"
                "(id, name, rules_json, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (smart_id, name, rules.model_dump_json(), position, now, now),
            )
        return get_smart_folder(smart_id) or {}
    finally:
        conn.close()


def update_smart_folder(
    smart_id: str, *, name: str | None = None, rules: SmartRules | None = None
) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            if not conn.execute(
                "SELECT 1 FROM note_smart_folders WHERE id = ?", (smart_id,)
            ).fetchone():
                raise SmartFolderNotFound(smart_id)
            if name is not None:
                conn.execute(
                    "UPDATE note_smart_folders SET name = ?, updated_at = ? WHERE id = ?",
                    (name, now_iso(), smart_id),
                )
            if rules is not None:
                conn.execute(
                    "UPDATE note_smart_folders SET rules_json = ?, updated_at = ? WHERE id = ?",
                    (rules.model_dump_json(), now_iso(), smart_id),
                )
        return get_smart_folder(smart_id) or {}
    finally:
        conn.close()


def delete_smart_folder(smart_id: str) -> bool:
    """Only the folder goes: its notes were never in it."""

    conn = _connect()
    try:
        with _tx(conn):
            return (
                conn.execute("DELETE FROM note_smart_folders WHERE id = ?", (smart_id,)).rowcount
                > 0
            )
    finally:
        conn.close()


# ------------------------------------------------------- tags across notes --

#: An inline #tag, exactly as the editor finds one (frontend/src/notes/tags.js).
_HASHTAG = re.compile(r"(^|[\s(\[{\"'])#([\w-]*[^\W\d_][\w-]*)")
_INLINE_CODE = re.compile(r"(`+)(.+?)\1")
_FENCE_LINE = re.compile(r"^\s*(```|~~~)")


def _tag_key(raw: str) -> str:
    return re.sub(r"\s+", "-", raw.strip().lower().lstrip("#"))[:40]


def _retag_text(text: str, old: str, new: str | None) -> str:
    def swap(match: re.Match) -> str:
        if _tag_key(match.group(2)) != old:
            return match.group(0)
        return match.group(1) + (f"#{new}" if new else "")

    return _HASHTAG.sub(swap, text)


def _retag_markdown(body: str, old: str, new: str | None) -> str:
    """Rewrite tags in Markdown, leaving code blocks and inline code alone."""

    out: list[str] = []
    fenced = False
    for line in body.split("\n"):
        if _FENCE_LINE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced:
            out.append(line)
            continue
        pieces, last = [], 0
        for code in _INLINE_CODE.finditer(line):
            pieces.append(_retag_text(line[last : code.start()], old, new))
            pieces.append(code.group(0))
            last = code.end()
        pieces.append(_retag_text(line[last:], old, new))
        out.append("".join(pieces))
    return "\n".join(out)


def _retag_doc(node: Any, old: str, new: str | None) -> Any:
    """Rewrite tags in an editor document, leaving code (blocks and marks) alone."""

    if not isinstance(node, dict):
        return node
    if node.get("type") == "codeBlock":
        return node
    if node.get("type") == "text":
        if any(
            isinstance(mark, dict) and mark.get("type") == "code"
            for mark in node.get("marks") or []
        ):
            return node
        return {**node, "text": _retag_text(node.get("text") or "", old, new)}
    children = node.get("content")
    if not isinstance(children, list):
        return node
    rewritten = [_retag_doc(child, old, new) for child in children]
    # A text node may not be empty in the editor's document.
    rewritten = [
        child
        for child in rewritten
        if not (isinstance(child, dict) and child.get("type") == "text" and not child.get("text"))
    ]
    return {**node, "content": rewritten}


def retag(old: str, new: str | None) -> list[dict]:
    """Rename a tag (``new``) or delete it (``None``) wherever it is written.

    Each note is rewritten in its own transaction, as a content write: its
    revision moves on, so a tab still typing into it meets a conflict on its
    next save and both versions are kept. Locked notes hold their tags inside
    their ciphertext and are left as they are. Their edit dates are kept: the
    list is not reshuffled by a rename.
    """

    conn = _connect()
    changed: list[dict] = []
    try:
        ids = [
            row[0]
            for row in conn.execute("SELECT DISTINCT note_id FROM note_tags WHERE tag = ?", (old,))
        ]
        for note_id in ids:
            with _tx(conn):
                row = conn.execute(
                    "SELECT id, revision, content_format, body, doc_json FROM notes WHERE id = ?",
                    (note_id,),
                ).fetchone()
                if row is None or row["content_format"] == "encrypted":
                    continue
                tags = [
                    r[0]
                    for r in conn.execute(
                        "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,)
                    )
                ]
                if old not in tags:
                    continue
                next_tags: list[str] = []
                for tag in tags:
                    tag = (new if tag == old else tag) or ""
                    if tag and tag not in next_tags:
                        next_tags.append(tag)
                body = _retag_markdown(row["body"] or "", old, new)
                doc = json.loads(row["doc_json"]) if row["doc_json"] else None
                if doc is not None:
                    doc = _retag_doc(doc, old, new)
                derived = (
                    derive_from_doc(doc, body) if doc is not None else derive_from_markdown(body)
                )
                updated = conn.execute(
                    "UPDATE notes SET body = ?, doc_json = ?, preview = ?, excerpt = ?, "
                    "revision = revision + 1 "
                    "WHERE id = ? AND revision = ?",
                    (
                        body,
                        json.dumps(doc) if doc is not None else None,
                        derived.preview,
                        derived.excerpt,
                        note_id,
                        row["revision"],
                    ),
                ).rowcount
                if not updated:
                    continue
                _replace_tags(conn, note_id, next_tags)
                changed.append({"id": note_id, "revision": row["revision"] + 1})
        return changed
    finally:
        conn.close()


# ----------------------------------------------------------- shared writes --


def _replace_tags(conn: sqlite3.Connection, note_id: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM note_tags WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT OR IGNORE INTO note_tags(note_id, tag) VALUES (?, ?)",
        [(note_id, tag) for tag in tags],
    )


def _replace_refs(
    conn: sqlite3.Connection, note_id: str, source: str, ref_id: str, file_ids: list[str] | tuple
) -> None:
    conn.execute(
        "DELETE FROM note_attachment_refs WHERE note_id = ? AND source = ? AND ref_id = ?",
        (note_id, source, ref_id),
    )
    now = now_iso()
    conn.executemany(
        "INSERT OR IGNORE INTO note_attachment_refs(file_id, note_id, source, ref_id, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [(file_id, note_id, source, ref_id, now) for file_id in dict.fromkeys(file_ids)],
    )


def _insert_version(
    conn: sqlite3.Connection, row: sqlite3.Row | dict, kind: str, tags: list[str]
) -> str:
    version_id = new_id()
    conn.execute(
        """
        INSERT INTO note_versions (
            id, note_id, kind, content_format, title, body, doc_json, tags_json, summary,
            cipher_json, lock_key_version, revision, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            row["id"],
            kind,
            row["content_format"],
            row["title"],
            row["body"],
            row["doc_json"],
            json.dumps(tags),
            row["summary"],
            row["cipher_json"],
            row["lock_key_version"],
            row["revision"],
            now_iso(),
        ),
    )
    if row["content_format"] == "markdown":
        file_ids = derive_from_markdown(row["body"] or "").attachment_file_ids
    else:
        file_ids = tuple(
            r["file_id"]
            for r in conn.execute(
                "SELECT file_id FROM note_attachment_refs WHERE note_id = ? AND source = 'doc'",
                (row["id"],),
            )
        )
    _replace_refs(conn, row["id"], "version", version_id, file_ids)
    return version_id


def _tags_of(conn: sqlite3.Connection, note_id: str) -> list[str]:
    return [
        r["tag"]
        for r in conn.execute(
            "SELECT tag FROM note_tags WHERE note_id = ? ORDER BY tag", (note_id,)
        )
    ]


def _capture_conflict(
    conn: sqlite3.Connection,
    *,
    note_id: str,
    reason: str,
    base_revision: int,
    server_revision: int,
    payload: dict,
) -> str:
    write_id = payload.get("write_id")
    if write_id:
        # A replayed write that loses again is the same conflict, not a new one.
        existing = conn.execute(
            "SELECT id FROM note_conflicts WHERE note_id = ? AND write_id = ?",
            (note_id, write_id),
        ).fetchone()
        if existing is not None:
            return existing["id"]
    conflict_id = new_id()
    conn.execute(
        """
        INSERT INTO note_conflicts (
            id, note_id, reason, base_revision, server_revision, write_id, content_format,
            title, body, doc_json, tags_json, summary, cipher_json, lock_key_version,
            attachment_ids_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conflict_id,
            note_id,
            reason,
            base_revision,
            server_revision,
            payload.get("write_id"),
            payload["format"],
            payload.get("title") or "",
            payload.get("body"),
            json.dumps(payload["doc"]) if payload.get("doc") is not None else None,
            json.dumps(payload.get("tags") or []),
            payload.get("summary"),
            json.dumps(payload["cipher"]) if payload.get("cipher") is not None else None,
            payload.get("lock_key_version"),
            json.dumps(list(payload.get("attachment_ids") or [])),
            now_iso(),
        ),
    )
    _replace_refs(conn, note_id, "conflict", conflict_id, list(payload.get("attachment_ids") or []))
    return conflict_id


def _write_receipt(conn: sqlite3.Connection, write_id: str | None, note_id: str, revision: int):
    if write_id:
        conn.execute(
            "INSERT OR IGNORE INTO note_write_receipts(write_id, note_id, revision, created_at) "
            "VALUES (?, ?, ?, ?)",
            (write_id, note_id, revision, now_iso()),
        )


def _receipt(conn: sqlite3.Connection, write_id: str | None) -> sqlite3.Row | None:
    if not write_id:
        return None
    return conn.execute(
        "SELECT note_id, revision FROM note_write_receipts WHERE write_id = ?", (write_id,)
    ).fetchone()


# ------------------------------------------------------------ create/write --


def insert_note(note: dict) -> tuple[dict, bool]:
    """Create a note; returns it and whether this was a replay of an earlier create.

    With a ``write_id`` already seen, nothing is written and the note that write
    created is returned, so a create retried after a lost reply never duplicates.
    """

    conn = _connect()
    try:
        with _tx(conn):
            receipt = _receipt(conn, note.get("write_id"))
            if receipt is not None:
                existing_id = receipt["note_id"]
            else:
                existing_id = None
                derived: Derived = note["derived"]
                conn.execute(
                    """
                    INSERT INTO notes (
                        id, title, body, summary, source_type, source_id, source_url,
                        source_title, source_metadata_json, pinned, archived, deleted,
                        created_at, updated_at, content_format, doc_json, revision,
                        meta_revision, meta_updated_at, folder_id, checklist_total,
                        checklist_done, has_images, has_files, has_sketches, has_tables,
                        has_links, cover_file_id, preview, excerpt
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?, ?, 0, 0, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        note["id"],
                        note["title"],
                        note["body"],
                        note.get("summary"),
                        note.get("source_type"),
                        note.get("source_id"),
                        note.get("source_url"),
                        note.get("source_title"),
                        json.dumps(note.get("source_metadata") or {}),
                        note["created_at"],
                        note["updated_at"],
                        note["content_format"],
                        json.dumps(note["doc"]) if note.get("doc") is not None else None,
                        note["updated_at"],
                        note.get("folder_id"),
                        *derived.columns().values(),
                    ),
                )
                _replace_tags(conn, note["id"], note.get("tags", []))
                _replace_refs(conn, note["id"], "doc", note["id"], derived.attachment_file_ids)
                _write_receipt(conn, note.get("write_id"), note["id"], 0)
        return (_fetch_note(conn, existing_id or note["id"]) or note), existing_id is not None
    finally:
        conn.close()


def write_content(note_id: str, payload: dict, derived: Derived) -> dict:
    """Apply an editor content write by compare-and-swap, or capture it as a conflict.

    Raises ``NoteGone`` (purged), ``NoteLocked`` (plaintext for an encrypted note,
    refused and *not* stored) or ``ConflictCaptured`` (stale or deleted).
    """

    conn = _connect()
    conflict: ConflictCaptured | None = None
    try:
        with _tx(conn):
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NoteGone(note_id)
            receipt = _receipt(conn, payload.get("write_id"))
            if receipt is not None:
                if receipt["note_id"] != note_id:
                    raise ValueError("write_id_reused")
            else:
                current_format = row["content_format"]
                if current_format == "encrypted" and payload["format"] != "encrypted":
                    raise NoteLocked(note_id)
                if payload["format"] == "encrypted" and current_format != "encrypted":
                    raise ValueError("lock_required")
                if row["revision"] != payload["base_revision"] or row["deleted"]:
                    reason = "deleted" if row["deleted"] else "stale"
                    conflict_id = _capture_conflict(
                        conn,
                        note_id=note_id,
                        reason=reason,
                        base_revision=payload["base_revision"],
                        server_revision=row["revision"],
                        payload=payload,
                    )
                    conflict = ConflictCaptured(conflict_id, reason, note_id)
                else:
                    converting = (current_format == "markdown" and payload["format"] == "rich") or (
                        current_format == "encrypted" and payload.get("keep_original")
                    )
                    if converting:
                        has_original = conn.execute(
                            "SELECT 1 FROM note_versions WHERE note_id = ? "
                            "AND kind = 'original_markdown'",
                            (note_id,),
                        ).fetchone()
                        if not has_original:
                            _insert_version(conn, row, "original_markdown", _tags_of(conn, note_id))
                    _apply_content(conn, note_id, payload, derived, row["revision"])
                    _write_receipt(conn, payload.get("write_id"), note_id, row["revision"] + 1)
        if conflict is not None:
            raise conflict
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def _apply_content(
    conn: sqlite3.Connection, note_id: str, payload: dict, derived: Derived, base_revision: int
) -> None:
    encrypted = payload["format"] == "encrypted"
    columns = derived.columns()
    assignments = ", ".join(f"{column} = ?" for column in columns)
    cursor = conn.execute(
        f"""
        UPDATE notes SET
            title = ?, body = ?, doc_json = ?, summary = ?, cipher_json = ?,
            lock_key_version = ?, content_format = ?, {assignments},
            revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ? AND deleted = 0
        """,
        (
            payload["title"],
            "" if encrypted else payload.get("body") or "",
            None if encrypted else json.dumps(payload["doc"]),
            None if encrypted else payload.get("summary"),
            json.dumps(payload["cipher"]) if encrypted else None,
            payload.get("lock_key_version") if encrypted else None,
            payload["format"],
            *columns.values(),
            now_iso(),
            note_id,
            base_revision,
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("compare_and_swap_lost_inside_transaction")
    _replace_tags(conn, note_id, [] if encrypted else payload.get("tags") or [])
    file_ids = payload.get("attachment_ids") if encrypted else derived.attachment_file_ids
    _replace_refs(conn, note_id, "doc", note_id, list(file_ids or []))


def update_legacy(note_id: str, updates: dict, derived: Derived | None) -> dict:
    """The original PATCH: field updates, optionally guarded by ``base_revision``.

    A body written this way becomes the note's content again: a rich note's
    document is kept as a version first, then the note returns to Markdown.
    """

    conn = _connect()
    conflict: ConflictCaptured | None = None
    try:
        with _tx(conn):
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ? AND deleted = 0", (note_id,)
            ).fetchone()
            if row is None:
                raise NoteNotFound(note_id)
            content_keys = {"title", "body", "tags", "summary"} & updates.keys()
            if row["content_format"] == "encrypted" and (
                {"body", "tags", "summary"} & updates.keys()
            ):
                raise NoteLocked(note_id)
            base = updates.get("base_revision")
            if content_keys and base is not None and base != row["revision"]:
                # A legacy PATCH supplies only the fields it changes. Capture a
                # complete candidate so resolving a stale title or tag edit does
                # not replace a rich document with its Markdown projection (or
                # discard the note's existing tags and attachments).
                replaces_body = "body" in updates
                fmt = "markdown" if replaces_body else row["content_format"]
                attachment_ids = (
                    list(derived.attachment_file_ids)
                    if replaces_body and derived is not None
                    else [
                        ref["file_id"]
                        for ref in conn.execute(
                            "SELECT file_id FROM note_attachment_refs "
                            "WHERE note_id = ? AND source = 'doc'",
                            (note_id,),
                        )
                    ]
                )
                payload = {
                    "format": fmt,
                    "write_id": None,
                    "title": updates.get("title", row["title"]),
                    "body": updates.get("body", row["body"]),
                    "doc": (
                        json.loads(row["doc_json"]) if fmt == "rich" and row["doc_json"] else None
                    ),
                    "cipher": (
                        json.loads(row["cipher_json"])
                        if fmt == "encrypted" and row["cipher_json"]
                        else None
                    ),
                    "lock_key_version": row["lock_key_version"] if fmt == "encrypted" else None,
                    "attachment_ids": attachment_ids,
                    "tags": updates.get("tags", _tags_of(conn, note_id)),
                    "summary": updates.get("summary", row["summary"]),
                }
                conflict = ConflictCaptured(
                    _capture_conflict(
                        conn,
                        note_id=note_id,
                        reason="stale",
                        base_revision=base,
                        server_revision=row["revision"],
                        payload=payload,
                    ),
                    "stale",
                    note_id,
                )
            else:
                assignments: list[str] = []
                params: list[Any] = []
                if "body" in updates and row["content_format"] == "rich":
                    _insert_version(
                        conn, row, "rich_before_markdown_write", _tags_of(conn, note_id)
                    )
                    assignments += ["doc_json = NULL", "content_format = 'markdown'"]
                for key in ("title", "body", "summary"):
                    if key in updates:
                        assignments.append(f"{key} = ?")
                        params.append(updates[key])
                if derived is not None:
                    for column, value in derived.columns().items():
                        assignments.append(f"{column} = ?")
                        params.append(value)
                source_keys = ("source_type", "source_id", "source_url", "source_title")
                for key in source_keys:
                    if key in updates:
                        assignments.append(f"{key} = ?")
                        params.append(updates[key])
                if "source_metadata" in updates:
                    assignments.append("source_metadata_json = ?")
                    params.append(json.dumps(updates["source_metadata"] or {}))
                now = now_iso()
                if content_keys:
                    assignments += ["revision = revision + 1", "updated_at = ?"]
                    params.append(now)
                elif assignments:
                    assignments += ["meta_revision = meta_revision + 1", "meta_updated_at = ?"]
                    params.append(now)
                if assignments:
                    conn.execute(
                        f"UPDATE notes SET {', '.join(assignments)} WHERE id = ?",
                        (*params, note_id),
                    )
                if "tags" in updates:
                    _replace_tags(conn, note_id, updates["tags"])
                if derived is not None:
                    _replace_refs(conn, note_id, "doc", note_id, derived.attachment_file_ids)
        if conflict is not None:
            raise conflict
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


# ---------------------------------------------------------------- metadata --


def update_metadata(note_id: str, fields: dict) -> dict:
    """Pinned, archived or folder. Never touches content, ``revision`` or ``updated_at``."""

    allowed = {"pinned", "archived", "folder_id"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"not metadata: {sorted(unknown)}")
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute("SELECT id, deleted FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None or row["deleted"]:
                raise NoteNotFound(note_id)
            if fields.get("folder_id"):
                if not conn.execute(
                    "SELECT 1 FROM note_folders WHERE id = ?", (fields["folder_id"],)
                ).fetchone():
                    raise FolderNotFound(fields["folder_id"])
            assignments = []
            params: list[Any] = []
            for key, value in fields.items():
                assignments.append(f"{key} = ?")
                params.append((1 if value else 0) if key in ("pinned", "archived") else value)
            assignments += ["meta_revision = meta_revision + 1", "meta_updated_at = ?"]
            params.append(now_iso())
            conn.execute(
                f"UPDATE notes SET {', '.join(assignments)} WHERE id = ?", (*params, note_id)
            )
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def move_to_trash(note_id: str) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute("SELECT id, deleted FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NoteNotFound(note_id)
            if not row["deleted"]:
                now = now_iso()
                conn.execute(
                    "UPDATE notes SET deleted = 1, deleted_at = ?, "
                    "meta_revision = meta_revision + 1, meta_updated_at = ? "
                    "WHERE id = ? AND deleted = 0",
                    (now, now, note_id),
                )
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def restore_from_trash(note_id: str, folder_id: str | None = None) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute(
                "SELECT id, deleted, deleted_at, folder_id FROM notes WHERE id = ?", (note_id,)
            ).fetchone()
            if row is None or (row["deleted"] and row["deleted_at"] is None):
                raise NoteNotFound(note_id)
            if row["deleted"]:
                target = folder_id or row["folder_id"]
                if (
                    target
                    and not conn.execute(
                        "SELECT 1 FROM note_folders WHERE id = ?", (target,)
                    ).fetchone()
                ):
                    target = None
                conn.execute(
                    "UPDATE notes SET deleted = 0, deleted_at = NULL, folder_id = ?, "
                    "meta_revision = meta_revision + 1, meta_updated_at = ? WHERE id = ?",
                    (target, now_iso(), note_id),
                )
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def permanently_delete(note_id: str, *, require_trashed: bool = True) -> bool:
    """Remove a note and every row that points at it, in one transaction.

    Returns False when the note was already gone. Attachment files the note made
    are queued for collection; they are removed only once nothing refers to them.
    """

    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute("SELECT id, deleted FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                return False
            if require_trashed and not row["deleted"]:
                raise NotInTrash(note_id)
            file_ids = [
                r["file_id"]
                for r in conn.execute(
                    "SELECT file_id FROM note_attachment_refs WHERE note_id = ?", (note_id,)
                )
            ]
            if _has_table(conn, "workspace_file_links"):
                file_ids += [
                    r["file_id"]
                    for r in conn.execute(
                        "SELECT file_id FROM workspace_file_links "
                        "WHERE link_type = 'note' AND target_id = ?",
                        (note_id,),
                    )
                ]
                conn.execute(
                    "DELETE FROM workspace_file_links WHERE link_type = 'note' AND target_id = ?",
                    (note_id,),
                )
            for table in ("workspace_project_notes", "workspace_task_notes"):
                if _has_table(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE note_id = ?", (note_id,))
            if _has_table(conn, "workspace_artifacts"):
                conn.execute(
                    "UPDATE workspace_artifacts SET note_id = NULL WHERE note_id = ?", (note_id,)
                )
            for table in (
                "note_tags",
                "note_links",
                "note_attachment_refs",
                "note_versions",
                "note_conflicts",
                "note_transitions",
                "note_write_receipts",
            ):
                conn.execute(f"DELETE FROM {table} WHERE note_id = ?", (note_id,))
            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            if file_ids:
                _enqueue(conn, "collect_files", {"file_ids": list(dict.fromkeys(file_ids))})
    finally:
        conn.close()
    run_pending_operations()
    return True


def trashed_note_ids(*, older_than: datetime | None = None) -> list[str]:
    conn = _connect()
    try:
        sql = "SELECT id FROM notes WHERE deleted = 1 AND deleted_at IS NOT NULL"
        params: list[Any] = []
        if older_than is not None:
            sql += " AND deleted_at < ?"
            params.append(older_than.isoformat())
        return [row["id"] for row in conn.execute(sql, params)]
    finally:
        conn.close()


def duplicate_note(note_id: str, *, title: str | None = None) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            row = conn.execute(
                "SELECT * FROM notes WHERE id = ? AND deleted = 0", (note_id,)
            ).fetchone()
            if row is None:
                raise NoteNotFound(note_id)
            if row["content_format"] == "encrypted":
                raise NoteLocked(note_id)
            copy_id = new_id()
            now = now_iso()
            conn.execute(
                f"""
                INSERT INTO notes (
                    id, title, body, summary, source_type, source_metadata_json, pinned, archived,
                    deleted, created_at, updated_at, content_format, doc_json, revision,
                    meta_revision, meta_updated_at, folder_id, {", ".join(_DERIVED_COLUMNS)}
                )
                SELECT ?, ?, body, summary, 'manual', '{{}}', 0, 0, 0, ?, ?, content_format,
                    doc_json, 0, 0, ?, folder_id, {", ".join(_DERIVED_COLUMNS)}
                FROM notes WHERE id = ?
                """,
                (copy_id, title or row["title"], now, now, now, note_id),
            )
            _replace_tags(conn, copy_id, _tags_of(conn, note_id))
            file_ids = [
                r["file_id"]
                for r in conn.execute(
                    "SELECT file_id FROM note_attachment_refs WHERE note_id = ? AND source = 'doc'",
                    (note_id,),
                )
            ]
            _replace_refs(conn, copy_id, "doc", copy_id, file_ids)
            if file_ids and _has_table(conn, "workspace_file_links"):
                for file_id in file_ids:
                    conn.execute(
                        "INSERT OR IGNORE INTO workspace_file_links "
                        "(id, file_id, link_type, target_id, title, metadata_json, created_at) "
                        "SELECT ?, ?, 'note', ?, ?, '{}', ? "
                        "WHERE EXISTS (SELECT 1 FROM workspace_files WHERE id = ?)",
                        (new_id(), file_id, copy_id, title or row["title"], now, file_id),
                    )
        return _fetch_note(conn, copy_id) or {}
    finally:
        conn.close()


def insert_link(link: dict) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            conn.execute(
                """
                INSERT INTO note_links (
                    id, note_id, link_type, target_id, target_url, title,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link["id"],
                    link["note_id"],
                    link["link_type"],
                    link.get("target_id"),
                    link.get("target_url"),
                    link.get("title"),
                    json.dumps(link.get("metadata", {})),
                    link["created_at"],
                ),
            )
        return link
    finally:
        conn.close()


# ----------------------------------------------------------------- folders --


def _folder_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["position"] = float(data.get("position") or 0)
    return data


def list_folders() -> list[dict]:
    conn = _connect()
    try:
        return [
            _folder_row(row)
            for row in conn.execute("SELECT * FROM note_folders ORDER BY position, lower(name)")
        ]
    finally:
        conn.close()


def get_folder(folder_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM note_folders WHERE id = ?", (folder_id,)).fetchone()
        return _folder_row(row) if row else None
    finally:
        conn.close()


def create_folder(name: str, parent_id: str | None) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            if (
                parent_id
                and not conn.execute(
                    "SELECT 1 FROM note_folders WHERE id = ?", (parent_id,)
                ).fetchone()
            ):
                raise FolderNotFound(parent_id)
            position = conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM note_folders WHERE parent_id IS ?",
                (parent_id,),
            ).fetchone()[0]
            folder_id = new_id()
            now = now_iso()
            conn.execute(
                "INSERT INTO note_folders(id, name, parent_id, position, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (folder_id, name, parent_id, position, now, now),
            )
        return get_folder(folder_id) or {}
    finally:
        conn.close()


def _descendants(conn: sqlite3.Connection, folder_id: str) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM note_folders WHERE id = ?
                UNION ALL
                SELECT f.id FROM note_folders f JOIN tree ON f.parent_id = tree.id
            )
            SELECT id FROM tree
            """,
            (folder_id,),
        )
    ]


def update_folder(folder_id: str, updates: dict) -> dict:
    conn = _connect()
    try:
        with _tx(conn):
            if not conn.execute("SELECT 1 FROM note_folders WHERE id = ?", (folder_id,)).fetchone():
                raise FolderNotFound(folder_id)
            assignments: list[str] = []
            params: list[Any] = []
            if "name" in updates:
                assignments.append("name = ?")
                params.append(updates["name"])
            if "parent_id" in updates:
                parent = updates["parent_id"]
                if parent is not None:
                    if not conn.execute(
                        "SELECT 1 FROM note_folders WHERE id = ?", (parent,)
                    ).fetchone():
                        raise FolderNotFound(parent)
                    if parent in _descendants(conn, folder_id):
                        raise InvalidFolderMove("A folder cannot move inside itself.")
                assignments.append("parent_id = ?")
                params.append(parent)
            if "position" in updates:
                assignments.append("position = ?")
                params.append(updates["position"])
            if assignments:
                assignments.append("updated_at = ?")
                params.append(now_iso())
                conn.execute(
                    f"UPDATE note_folders SET {', '.join(assignments)} WHERE id = ?",
                    (*params, folder_id),
                )
        return get_folder(folder_id) or {}
    finally:
        conn.close()


def delete_folder(folder_id: str) -> dict:
    """Delete a folder and its subfolders; their notes go to Recently Deleted."""

    conn = _connect()
    try:
        with _tx(conn):
            ids = _descendants(conn, folder_id)
            if not ids:
                raise FolderNotFound(folder_id)
            placeholders = ",".join("?" * len(ids))
            now = now_iso()
            trashed = conn.execute(
                f"UPDATE notes SET deleted = 1, deleted_at = ?, meta_revision = meta_revision + 1, "
                f"meta_updated_at = ? WHERE folder_id IN ({placeholders}) AND deleted = 0",
                (now, now, *ids),
            ).rowcount
            conn.execute(f"DELETE FROM note_folders WHERE id IN ({placeholders})", ids)
        return {"deleted_folders": len(ids), "trashed_notes": trashed}
    finally:
        conn.close()


# ---------------------------------------------------- versions & conflicts --


def _version_row(row: sqlite3.Row, *, full: bool) -> dict:
    data = dict(row)
    doc_json = data.pop("doc_json", None)
    cipher_json = data.pop("cipher_json", None)
    tags_json = data.pop("tags_json", None)
    if full:
        data["doc"] = json.loads(doc_json) if doc_json else None
        data["cipher"] = json.loads(cipher_json) if cipher_json else None
        data["tags"] = json.loads(tags_json) if tags_json else []
    else:
        data.pop("body", None)
        data.pop("summary", None)
    return data


def list_versions(note_id: str) -> list[dict]:
    conn = _connect()
    try:
        return [
            _version_row(row, full=False)
            for row in conn.execute(
                "SELECT * FROM note_versions WHERE note_id = ? ORDER BY created_at DESC", (note_id,)
            )
        ]
    finally:
        conn.close()


def get_version(note_id: str, version_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM note_versions WHERE id = ? AND note_id = ?", (version_id, note_id)
        ).fetchone()
        return _version_row(row, full=True) if row else None
    finally:
        conn.close()


def restore_version(note_id: str, version_id: str, base_revision: int, write_id: str) -> dict:
    """Make a stored version the content again, keeping what it replaces as a version."""

    conn = _connect()
    try:
        with _tx(conn):
            receipt = _receipt(conn, write_id)
            if receipt is None:
                row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
                if row is None:
                    raise NoteGone(note_id)
                version = conn.execute(
                    "SELECT * FROM note_versions WHERE id = ? AND note_id = ?",
                    (version_id, note_id),
                ).fetchone()
                if version is None:
                    raise NoteNotFound(version_id)
                if row["deleted"]:
                    raise NoteDeleted(note_id)
                if row["revision"] != base_revision:
                    raise StaleRevision(note_id)
                if (version["content_format"] == "encrypted") != (
                    row["content_format"] == "encrypted"
                ):
                    raise NoteLocked(note_id)
                _insert_version(conn, row, "pre_restore", _tags_of(conn, note_id))
                if version["content_format"] == "rich" and version["doc_json"]:
                    from app.services.notes.content import derive_from_doc

                    derived = derive_from_doc(
                        json.loads(version["doc_json"]), version["body"] or ""
                    )
                elif version["content_format"] == "encrypted":
                    derived = Derived()
                else:
                    derived = derive_from_markdown(version["body"] or "")
                columns = derived.columns()
                assignments = ", ".join(f"{column} = ?" for column in columns)
                conn.execute(
                    f"""
                    UPDATE notes SET title = ?, body = ?, doc_json = ?, summary = ?,
                        cipher_json = ?, lock_key_version = ?, content_format = ?, {assignments},
                        revision = revision + 1, updated_at = ?
                    WHERE id = ? AND revision = ? AND deleted = 0
                    """,
                    (
                        version["title"],
                        version["body"] or "",
                        version["doc_json"],
                        version["summary"],
                        version["cipher_json"],
                        version["lock_key_version"],
                        version["content_format"],
                        *columns.values(),
                        now_iso(),
                        note_id,
                        base_revision,
                    ),
                )
                tags = json.loads(version["tags_json"] or "[]")
                _replace_tags(
                    conn, note_id, [] if version["content_format"] == "encrypted" else tags
                )
                refs = [
                    r["file_id"]
                    for r in conn.execute(
                        "SELECT file_id FROM note_attachment_refs WHERE note_id = ? "
                        "AND source = 'version' AND ref_id = ?",
                        (note_id, version_id),
                    )
                ]
                _replace_refs(conn, note_id, "doc", note_id, refs)
                _write_receipt(conn, write_id, note_id, base_revision + 1)
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def _conflict_row(row: sqlite3.Row) -> dict:
    data = dict(row)
    doc_json = data.pop("doc_json", None)
    cipher_json = data.pop("cipher_json", None)
    data["doc"] = json.loads(doc_json) if doc_json else None
    data["cipher"] = json.loads(cipher_json) if cipher_json else None
    data["tags"] = json.loads(data.pop("tags_json", None) or "[]")
    data["attachment_ids"] = json.loads(data.pop("attachment_ids_json", None) or "[]")
    return data


def list_conflicts(note_id: str, *, include_resolved: bool = False) -> list[dict]:
    conn = _connect()
    try:
        sql = "SELECT * FROM note_conflicts WHERE note_id = ?"
        if not include_resolved:
            sql += " AND resolved_at IS NULL"
        return [
            _conflict_row(row) for row in conn.execute(sql + " ORDER BY created_at", (note_id,))
        ]
    finally:
        conn.close()


def get_conflict(note_id: str, conflict_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM note_conflicts WHERE id = ? AND note_id = ?", (conflict_id, note_id)
        ).fetchone()
        return _conflict_row(row) if row else None
    finally:
        conn.close()


def resolve_conflict_keep_mine(
    note_id: str, conflict_id: str, base_revision: int, write_id: str, derived: Derived
) -> dict:
    """The captured write becomes the content; the version it displaces is kept.

    ``derived`` is computed by the caller from the captured payload, which never
    changes once stored.
    """

    conn = _connect()
    try:
        with _tx(conn):
            if _receipt(conn, write_id) is None:
                conflict = conn.execute(
                    "SELECT * FROM note_conflicts WHERE id = ? AND note_id = ? "
                    "AND resolved_at IS NULL",
                    (conflict_id, note_id),
                ).fetchone()
                if conflict is None:
                    raise NoteNotFound(conflict_id)
                row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
                if row is None:
                    raise NoteGone(note_id)
                if row["deleted"]:
                    raise NoteDeleted(note_id)
                if row["revision"] != base_revision:
                    raise StaleRevision(note_id)
                captured = _conflict_row(conflict)
                fmt = conflict["content_format"]
                if (fmt == "encrypted") != (row["content_format"] == "encrypted"):
                    raise NoteLocked(note_id)
                _insert_version(conn, row, "conflict_displaced", _tags_of(conn, note_id))
                if fmt == "markdown":
                    columns = derived.columns()
                    assignments = ", ".join(f"{column} = ?" for column in columns)
                    conn.execute(
                        f"""
                        UPDATE notes SET title = ?, body = ?, summary = ?, doc_json = NULL,
                            content_format = 'markdown', {assignments},
                            revision = revision + 1, updated_at = ?
                        WHERE id = ? AND revision = ? AND deleted = 0
                        """,
                        (
                            captured["title"],
                            captured.get("body") or "",
                            captured.get("summary"),
                            *columns.values(),
                            now_iso(),
                            note_id,
                            base_revision,
                        ),
                    )
                    _replace_tags(conn, note_id, captured["tags"])
                    _replace_refs(conn, note_id, "doc", note_id, derived.attachment_file_ids)
                else:
                    _apply_content(
                        conn,
                        note_id,
                        {
                            "format": fmt,
                            "title": captured["title"],
                            "body": captured.get("body") or "",
                            "doc": captured["doc"],
                            "tags": captured["tags"],
                            "summary": captured.get("summary"),
                            "cipher": captured["cipher"],
                            "lock_key_version": captured.get("lock_key_version"),
                            "attachment_ids": captured["attachment_ids"],
                        },
                        derived,
                        base_revision,
                    )
                conn.execute(
                    "UPDATE note_conflicts SET resolved_at = ?, resolution = 'keep_mine' "
                    "WHERE id = ?",
                    (now_iso(), conflict_id),
                )
                conn.execute(
                    "DELETE FROM note_attachment_refs WHERE note_id = ? AND source = 'conflict' "
                    "AND ref_id = ?",
                    (note_id, conflict_id),
                )
                _write_receipt(conn, write_id, note_id, base_revision + 1)
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def resolve_conflict_use_other(note_id: str, conflict_id: str) -> dict:
    """Keep the current content; the captured write is kept as a version."""

    conn = _connect()
    try:
        with _tx(conn):
            conflict = conn.execute(
                "SELECT * FROM note_conflicts WHERE id = ? AND note_id = ? AND resolved_at IS NULL",
                (conflict_id, note_id),
            ).fetchone()
            if conflict is None:
                raise NoteNotFound(conflict_id)
            version_id = new_id()
            conn.execute(
                """
                INSERT INTO note_versions (
                    id, note_id, kind, content_format, title, body, doc_json, tags_json, summary,
                    cipher_json, lock_key_version, revision, created_at
                ) VALUES (?, ?, 'conflict_mine', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    note_id,
                    conflict["content_format"],
                    conflict["title"],
                    conflict["body"],
                    conflict["doc_json"],
                    conflict["tags_json"],
                    conflict["summary"],
                    conflict["cipher_json"],
                    conflict["lock_key_version"],
                    conflict["base_revision"],
                    now_iso(),
                ),
            )
            _replace_refs(
                conn,
                note_id,
                "version",
                version_id,
                json.loads(conflict["attachment_ids_json"] or "[]"),
            )
            conn.execute(
                "UPDATE note_conflicts SET resolved_at = ?, resolution = 'use_other' WHERE id = ?",
                (now_iso(), conflict_id),
            )
            conn.execute(
                "DELETE FROM note_attachment_refs WHERE note_id = ? AND source = 'conflict' "
                "AND ref_id = ?",
                (note_id, conflict_id),
            )
        return _fetch_note(conn, note_id) or {}
    finally:
        conn.close()


def resolve_conflict_save_copy(note_id: str, conflict_id: str, copy: dict) -> dict:
    """Create a separate note from the captured write and close the conflict, atomically."""

    conn = _connect()
    try:
        with _tx(conn):
            conflict = conn.execute(
                "SELECT * FROM note_conflicts WHERE id = ? AND note_id = ? AND resolved_at IS NULL",
                (conflict_id, note_id),
            ).fetchone()
            if conflict is None:
                raise NoteNotFound(conflict_id)
            if conflict["content_format"] == "encrypted":
                raise NoteLocked(note_id)
            folder = conn.execute("SELECT folder_id FROM notes WHERE id = ?", (note_id,)).fetchone()
            folder_id = folder["folder_id"] if folder else None
            if (
                folder_id
                and not conn.execute(
                    "SELECT 1 FROM note_folders WHERE id = ?", (folder_id,)
                ).fetchone()
            ):
                folder_id = None
            derived: Derived = copy["derived"]
            now = now_iso()
            conn.execute(
                f"""
                INSERT INTO notes (
                    id, title, body, summary, source_type, source_metadata_json, pinned,
                    archived, deleted, created_at, updated_at, content_format, doc_json,
                    revision, meta_revision, meta_updated_at, folder_id,
                    {", ".join(_DERIVED_COLUMNS)}
                ) VALUES (
                    ?, ?, ?, ?, 'manual', '{{}}', 0, 0, 0, ?, ?, ?, ?, 0, 0, ?, ?,
                    {", ".join("?" * len(_DERIVED_COLUMNS))}
                )
                """,
                (
                    copy["id"],
                    copy["title"],
                    conflict["body"] or "",
                    conflict["summary"],
                    now,
                    now,
                    conflict["content_format"],
                    conflict["doc_json"],
                    now,
                    folder_id,
                    *derived.columns().values(),
                ),
            )
            _replace_tags(conn, copy["id"], json.loads(conflict["tags_json"] or "[]"))
            _replace_refs(conn, copy["id"], "doc", copy["id"], derived.attachment_file_ids)
            conn.execute(
                "UPDATE note_conflicts SET resolved_at = ?, resolution = 'save_copy' WHERE id = ?",
                (now, conflict_id),
            )
            conn.execute(
                "DELETE FROM note_attachment_refs WHERE note_id = ? AND source = 'conflict' "
                "AND ref_id = ?",
                (note_id, conflict_id),
            )
        return _fetch_note(conn, copy["id"]) or {}
    finally:
        conn.close()


def mark_conflict_resolved(note_id: str, conflict_id: str, resolution: str) -> None:
    conn = _connect()
    try:
        with _tx(conn):
            updated = conn.execute(
                "UPDATE note_conflicts SET resolved_at = ?, resolution = ? "
                "WHERE id = ? AND note_id = ? AND resolved_at IS NULL",
                (now_iso(), resolution, conflict_id, note_id),
            ).rowcount
            if not updated:
                raise NoteNotFound(conflict_id)
    finally:
        conn.close()


# ---------------------------------------------------------------- settings --


def get_setting(key: str) -> Any:
    conn = _connect()
    try:
        row = conn.execute("SELECT value_json FROM note_settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None
    finally:
        conn.close()


def set_setting(key: str, value: Any) -> None:
    conn = _connect()
    try:
        with _tx(conn):
            conn.execute(
                "INSERT INTO note_settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
                "updated_at = excluded.updated_at",
                (key, json.dumps(value), now_iso()),
            )
    finally:
        conn.close()


# ------------------------------------------------- operations & collection --


def _enqueue(conn: sqlite3.Connection, kind: str, payload: dict) -> str:
    op_id = new_id()
    now = now_iso()
    conn.execute(
        "INSERT INTO note_operations(id, kind, payload_json, state, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (op_id, kind, json.dumps(payload), now, now),
    )
    return op_id


def _files_root() -> Path:
    return Path(get_settings().workspace_files_dir).expanduser().resolve()


def _file_is_referenced(conn: sqlite3.Connection, file_id: str) -> bool:
    if conn.execute(
        "SELECT 1 FROM note_attachment_refs WHERE file_id = ? LIMIT 1", (file_id,)
    ).fetchone():
        return True
    if (
        _has_table(conn, "workspace_file_links")
        and conn.execute(
            "SELECT 1 FROM workspace_file_links WHERE file_id = ? LIMIT 1", (file_id,)
        ).fetchone()
    ):
        return True
    if (
        _has_table(conn, "gallery_items")
        and conn.execute(
            "SELECT 1 FROM gallery_items WHERE file_id = ? LIMIT 1", (file_id,)
        ).fetchone()
    ):
        return True
    return False


def _collect_file(conn: sqlite3.Connection, file_id: str) -> str | None:
    """Delete one Notes-made file row if nothing refers to it; return its path to unlink."""

    if not _has_table(conn, "workspace_files"):
        return None
    row = conn.execute(
        "SELECT id, storage_path, source_type FROM workspace_files WHERE id = ?", (file_id,)
    ).fetchone()
    if row is None or row["source_type"] not in NOTE_FILE_SOURCES:
        return None
    if _file_is_referenced(conn, file_id):
        return None
    path = Path(row["storage_path"]).expanduser().resolve()
    root = _files_root()
    if not path.is_relative_to(root):
        logger.warning("Refusing to delete a note file outside the files root: %s", path)
        return None
    conn.execute("DELETE FROM workspace_files WHERE id = ?", (file_id,))
    return str(path)


def run_pending_operations() -> int:
    """Finish queued disk work (file collection). Safe to run any number of times."""

    conn = _connect()
    done = 0
    try:
        ops = conn.execute(
            "SELECT * FROM note_operations WHERE state = 'pending' ORDER BY created_at"
        ).fetchall()
        for op in ops:
            payload = json.loads(op["payload_json"])
            try:
                if op["kind"] == "collect_files":
                    with _tx(conn):
                        paths = list(payload.get("paths") or [])
                        for file_id in payload.get("file_ids") or []:
                            path = _collect_file(conn, file_id)
                            if path:
                                paths.append(path)
                        payload = {"file_ids": [], "paths": paths}
                        conn.execute(
                            "UPDATE note_operations SET payload_json = ?, updated_at = ? "
                            "WHERE id = ?",
                            (json.dumps(payload), now_iso(), op["id"]),
                        )
                    for path in payload["paths"]:
                        Path(path).unlink(missing_ok=True)
                with _tx(conn):
                    conn.execute(
                        "UPDATE note_operations SET state = 'done', updated_at = ? WHERE id = ?",
                        (now_iso(), op["id"]),
                    )
                done += 1
            except Exception as exc:  # noqa: BLE001 - left pending and retried later
                logger.exception("Notes operation %s failed", op["id"])
                conn.execute(
                    "UPDATE note_operations SET attempts = attempts + 1, last_error = ?, "
                    "updated_at = ? WHERE id = ?",
                    (str(exc)[:500], now_iso(), op["id"]),
                )
        return done
    finally:
        conn.close()


def collect_garbage(*, now: datetime | None = None) -> int:
    """Queue every Notes-made file nothing refers to (after a grace period)."""

    now = now or datetime.now(UTC)
    paused = get_setting("gc_paused_until")
    if paused and datetime.fromisoformat(paused) > now:
        return 0
    conn = _connect()
    try:
        if not _has_table(conn, "workspace_files"):
            return 0
        cutoff = (now - GC_GRACE).isoformat()
        candidates = [
            row["id"]
            for row in conn.execute(
                f"""
                SELECT id FROM workspace_files
                WHERE source_type IN ({",".join("?" * len(NOTE_FILE_SOURCES))})
                AND created_at < ?
                """,
                (*NOTE_FILE_SOURCES, cutoff),
            )
            if not _file_is_referenced(conn, row["id"])
        ]
        if candidates:
            with _tx(conn):
                _enqueue(conn, "collect_files", {"file_ids": candidates})
    finally:
        conn.close()
    if candidates:
        run_pending_operations()
    return len(candidates)


def run_maintenance(*, now: datetime | None = None, force: bool = False) -> None:
    """Replay unfinished work; at most daily, purge expired trash and collect files."""

    from app.services.notes import transitions

    now = now or datetime.now(UTC)
    run_pending_operations()
    # Every start, not daily: a lock its tab never finished holds staged files.
    transitions.sweep(now=now)
    last = get_setting("maintenance_last_run")
    if not force and last and now - datetime.fromisoformat(last) < MAINTENANCE_INTERVAL:
        return
    for note_id in trashed_note_ids(older_than=now - TRASH_RETENTION):
        permanently_delete(note_id)
    conn = _connect()
    try:
        with _tx(conn):
            conn.execute(
                "DELETE FROM note_write_receipts WHERE created_at < ?",
                ((now - RECEIPT_RETENTION).isoformat(),),
            )
    finally:
        conn.close()
    collect_garbage(now=now)
    set_setting("maintenance_last_run", now.isoformat())
