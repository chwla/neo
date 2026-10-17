"""Versioned, backed-up schema changes for Notes, and the way back.

Every change to the notes tables is a named step recorded in ``note_migrations``.
A step runs inside ``BEGIN IMMEDIATE`` together with its ledger row and checks its
own preconditions, so it is idempotent: a crash leaves it either fully applied or
not at all, and two processes starting at once cannot both apply it.

Before the first pending step touches a database that already holds notes, the
whole database is copied with SQLite's online backup API, verified and
fingerprinted. No verified copy, no migration: the failure is recorded and Notes
reports it, while every other feature keeps working.

``restore`` puts the notes tables back from such a copy. Other features point at
notes too (project and task links, file links, artifacts), so a restore checks
those references before, inside and after its transaction, removes only the ones
that would dangle, writes down exactly what it removed, and keeps a safety copy
of the database it replaced so the restore itself can be undone.

Command line:
    python -m app.services.notes.migrations restore --profile <id> --backup <file.db>
    python -m app.services.notes.migrations restore --database <neo.db> --backup <file.db>
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.core.sqlite_backup import backup_sqlite_file, file_sha256
from app.services.notes.content import derive_from_markdown

logger = logging.getLogger(__name__)

GC_PAUSE_AFTER_RESTORE = timedelta(days=30)

#: Tables this module owns. A restore replaces exactly these and nothing else.
NOTES_OWNED_TABLES = (
    "notes",
    "note_tags",
    "note_links",
    "note_folders",
    "note_smart_folders",
    "note_settings",
    "note_key_wraps",
    "note_versions",
    "note_conflicts",
    "note_attachment_refs",
    "note_operations",
    "note_transitions",
    "note_write_receipts",
    "note_migrations",
)

#: Columns added to ``notes`` after the original schema, with their SQL types.
ADDED_NOTE_COLUMNS = {
    "content_format": "TEXT NOT NULL DEFAULT 'markdown'",
    "doc_json": "TEXT",
    "revision": "INTEGER NOT NULL DEFAULT 0",
    "meta_revision": "INTEGER NOT NULL DEFAULT 0",
    "meta_updated_at": "TEXT",
    "folder_id": "TEXT",
    "deleted_at": "TEXT",
    "cipher_json": "TEXT",
    "lock_key_version": "INTEGER",
    "checklist_total": "INTEGER NOT NULL DEFAULT 0",
    "checklist_done": "INTEGER NOT NULL DEFAULT 0",
    "has_images": "INTEGER NOT NULL DEFAULT 0",
    "has_files": "INTEGER NOT NULL DEFAULT 0",
    "has_sketches": "INTEGER NOT NULL DEFAULT 0",
    "has_tables": "INTEGER NOT NULL DEFAULT 0",
    "has_links": "INTEGER NOT NULL DEFAULT 0",
    "cover_file_id": "TEXT",
    "preview": "TEXT NOT NULL DEFAULT ''",
    "excerpt": "TEXT NOT NULL DEFAULT ''",
}

NOTE_SETTINGS_SQL = """
    CREATE TABLE IF NOT EXISTS note_settings (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
"""


class NotesMigrationError(RuntimeError):
    """A migration could not run safely; the database was left as it was."""


class NotesRestoreError(RuntimeError):
    """A restore was refused or rolled back; the database was left as it was."""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    """A connection whose transactions are opened explicitly, never implicitly."""

    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None, uri=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn: sqlite3.Connection, name: str, schema: str = "main") -> bool:
    row = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


# --------------------------------------------------------------------- steps --


def _step_base_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            summary TEXT,
            source_type TEXT,
            source_id TEXT,
            source_url TEXT,
            source_title TEXT,
            source_metadata_json TEXT,
            pinned INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_tags (
            note_id TEXT NOT NULL,
            tag TEXT NOT NULL,
            PRIMARY KEY (note_id, tag),
            FOREIGN KEY (note_id) REFERENCES notes(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_links (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            link_type TEXT NOT NULL,
            target_id TEXT,
            target_url TEXT,
            title TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source_type, source_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_visible_sort "
        "ON notes(deleted, archived, pinned, updated_at DESC)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag)")


def _step_note_columns(conn: sqlite3.Connection) -> None:
    existing = _columns(conn, "notes")
    for column, spec in ADDED_NOTE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {column} {spec}")


def _step_note_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id TEXT REFERENCES note_folders(id),
            position REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_smart_folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rules_json TEXT NOT NULL,
            position REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(NOTE_SETTINGS_SQL)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_key_wraps (
            id TEXT PRIMARY KEY,
            key_version INTEGER NOT NULL,
            kdf TEXT NOT NULL,
            salt TEXT NOT NULL,
            iterations INTEGER NOT NULL,
            wrapped_dek TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_versions (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL REFERENCES notes(id),
            kind TEXT NOT NULL,
            content_format TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            doc_json TEXT,
            tags_json TEXT,
            summary TEXT,
            cipher_json TEXT,
            lock_key_version INTEGER,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_conflicts (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL REFERENCES notes(id),
            reason TEXT NOT NULL,
            base_revision INTEGER NOT NULL,
            server_revision INTEGER NOT NULL,
            write_id TEXT,
            content_format TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT,
            doc_json TEXT,
            tags_json TEXT,
            summary TEXT,
            cipher_json TEXT,
            lock_key_version INTEGER,
            attachment_ids_json TEXT,
            created_at TEXT NOT NULL,
            resolved_at TEXT,
            resolution TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_attachment_refs (
            file_id TEXT NOT NULL,
            note_id TEXT NOT NULL REFERENCES notes(id),
            source TEXT NOT NULL,
            ref_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (file_id, source, ref_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_operations (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_transitions (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL REFERENCES notes(id),
            kind TEXT NOT NULL,
            base_revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            staged_files_json TEXT NOT NULL DEFAULT '[]',
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS note_write_receipts (
            write_id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL REFERENCES notes(id),
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)


def _step_indexes(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_id)",
        "CREATE INDEX IF NOT EXISTS idx_notes_trash ON notes(deleted, deleted_at)",
        "CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_note_folders_parent ON note_folders(parent_id)",
        "CREATE INDEX IF NOT EXISTS idx_note_versions_note ON note_versions(note_id)",
        "CREATE INDEX IF NOT EXISTS idx_note_conflicts_open "
        "ON note_conflicts(note_id, resolved_at)",
        "CREATE INDEX IF NOT EXISTS idx_note_attachment_refs_note ON note_attachment_refs(note_id)",
        "CREATE INDEX IF NOT EXISTS idx_note_operations_state ON note_operations(state)",
        "CREATE INDEX IF NOT EXISTS idx_note_transitions_note ON note_transitions(note_id, state)",
        "CREATE INDEX IF NOT EXISTS idx_note_write_receipts_note ON note_write_receipts(note_id)",
    ):
        conn.execute(statement)


def _step_backfill(conn: sqlite3.Connection) -> None:
    """Derived list fields for every existing note, computed from its Markdown."""

    rows = conn.execute("SELECT id, body, updated_at FROM notes").fetchall()
    for row in rows:
        derived = derive_from_markdown(row["body"] or "")
        values = derived.columns()
        assignments = ", ".join(f"{column} = ?" for column in values)
        conn.execute(
            f"UPDATE notes SET {assignments}, meta_updated_at = COALESCE(meta_updated_at, ?) "
            "WHERE id = ?",
            (*values.values(), row["updated_at"], row["id"]),
        )


@dataclass(frozen=True)
class Step:
    id: str
    apply: Callable[[sqlite3.Connection], None]


STEPS: list[Step] = [
    Step("0001_base_schema", _step_base_schema),
    Step("0002_note_columns", _step_note_columns),
    Step("0003_note_tables", _step_note_tables),
    Step("0004_note_indexes", _step_indexes),
    Step("0005_backfill_derived", _step_backfill),
]

# ------------------------------------------------------------------- backups --

#: Databases whose migration was refused, by path, with the reason. Notes reads
#: this to answer "upgrade failed, your notes are unchanged" instead of crashing.
_FAILED: dict[str, str] = {}


def failure_for(db_path: str | Path) -> str | None:
    return _FAILED.get(str(Path(db_path).resolve()))


def clear_failure(db_path: str | Path) -> None:
    _FAILED.pop(str(Path(db_path).resolve()), None)


def _profiles_root() -> Path:
    from app.services.profile_accounts import profiles_root

    return profiles_root()


def database_label(db_path: str | Path) -> tuple[str, bool]:
    """A name for the database's backup, and whether it belongs to a guest."""

    path = Path(db_path).resolve()
    section = path.parent.parent.name
    if section in ("accounts", "guests"):
        return path.parent.name, section == "guests"
    return "base", False


def _needs_backup(conn: sqlite3.Connection, db_path: str | Path) -> bool:
    _, guest = database_label(db_path)
    if guest or not table_exists(conn, "notes"):
        return False
    return conn.execute("SELECT EXISTS (SELECT 1 FROM notes)").fetchone()[0] == 1


def write_backup(db_path: str | Path, folder: str, reason: str) -> dict:
    """Back up the whole database into ``backups/<folder>/`` with a manifest beside it."""

    label, _ = database_label(db_path)
    destination = _profiles_root() / "backups" / folder / f"{label}.db"
    backup = backup_sqlite_file(Path(db_path), destination)
    manifest = {
        "reason": reason,
        "label": label,
        "source_path": backup.source_path,
        "backup_path": backup.destination_path,
        "sha256": backup.sha256,
        "integrity": list(backup.integrity_result),
        "created_at": now_iso(),
    }
    manifest_path = destination.with_name(f"{label}.manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    manifest_path.chmod(0o600)
    return manifest


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


# --------------------------------------------------------------------- run --


def run(db_path: str | Path) -> list[str]:
    """Apply every pending step; return the ids applied. Raises on refusal."""

    db_path = Path(db_path).resolve()
    conn = connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS note_migrations "
            "(step TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {row["step"] for row in conn.execute("SELECT step FROM note_migrations")}
        pending = [step for step in STEPS if step.id not in applied]
        if not pending:
            return []
        if _needs_backup(conn, db_path):
            try:
                manifest = write_backup(db_path, f"notes-v2-{_timestamp()}", "notes-v2-migration")
            except Exception as exc:  # noqa: BLE001 - any failure means "do not migrate"
                raise NotesMigrationError(f"backup_failed: {exc}") from exc
            logger.info("Notes backup written before migrating: %s", manifest["backup_path"])

        done: list[str] = []
        for step in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                already = conn.execute(
                    "SELECT 1 FROM note_migrations WHERE step = ?", (step.id,)
                ).fetchone()
                if not already:
                    step.apply(conn)
                    conn.execute(
                        "INSERT INTO note_migrations(step, applied_at) VALUES (?, ?)",
                        (step.id, now_iso()),
                    )
                    done.append(step.id)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return done
    finally:
        conn.close()


def run_safely(db_path: str | Path) -> bool:
    """``run``, with failures recorded instead of raised. Returns success."""

    key = str(Path(db_path).resolve())
    try:
        run(db_path)
    except Exception as exc:  # noqa: BLE001 - recorded and reported by the notes API
        logger.exception("Notes migration refused for %s", key)
        _FAILED[key] = str(exc)
        return False
    _FAILED.pop(key, None)
    return True


# ------------------------------------------------------------------ restore --


@dataclass
class RestoreReport:
    database: str
    backup: str
    safety_backup: str = ""
    report_path: str = ""
    lost_note_ids: list[str] = field(default_factory=list)
    changed_note_ids: list[str] = field(default_factory=list)
    removed: dict[str, list[dict]] = field(default_factory=dict)
    relinked: dict[str, list[dict]] = field(default_factory=dict)
    fk_before: list[dict] = field(default_factory=list)
    fk_after: list[dict] = field(default_factory=list)
    gc_paused_until: str = ""
    restored: bool = False


def _fk_violations(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"table": row[0], "rowid": row[1], "parent": row[2], "fkid": row[3]}
        for row in conn.execute("PRAGMA foreign_key_check").fetchall()
    ]


def _note_ids(conn: sqlite3.Connection, schema: str) -> set[str]:
    if not table_exists(conn, "notes", schema):
        return set()
    return {row[0] for row in conn.execute(f"SELECT id FROM {schema}.notes")}


#: Rows in other features that point at a note: (label, table, select-where-dangling).
def _dangling_queries(ids_sql: str) -> list[tuple[str, str, str]]:
    return [
        (
            "project_links",
            "workspace_project_notes",
            f"SELECT project_id, note_id, created_at FROM workspace_project_notes "
            f"WHERE note_id NOT IN ({ids_sql})",
        ),
        (
            "task_links",
            "workspace_task_notes",
            f"SELECT task_id, note_id, created_at FROM workspace_task_notes "
            f"WHERE note_id NOT IN ({ids_sql})",
        ),
        (
            "file_links",
            "workspace_file_links",
            f"SELECT id, file_id, target_id, created_at FROM workspace_file_links "
            f"WHERE link_type = 'note' AND target_id NOT IN ({ids_sql})",
        ),
        (
            "artifact_note_refs",
            "workspace_artifacts",
            f"SELECT id, note_id FROM workspace_artifacts "
            f"WHERE note_id IS NOT NULL AND note_id NOT IN ({ids_sql})",
        ),
    ]


def cross_service_violations(conn: sqlite3.Connection) -> list[dict]:
    """References into Notes that do not resolve, across every feature that keeps one."""

    found: list[dict] = []
    if not table_exists(conn, "notes"):
        return found
    for label, table, query in _dangling_queries("SELECT id FROM notes"):
        if table_exists(conn, table):
            for row in conn.execute(query).fetchall():
                found.append({"kind": label, **dict(row)})
    owned_children = (
        "note_tags",
        "note_links",
        "note_versions",
        "note_conflicts",
        "note_attachment_refs",
        "note_transitions",
        "note_write_receipts",
    )
    for table in owned_children:
        if table_exists(conn, table):
            for row in conn.execute(
                f"SELECT rowid, note_id FROM {table} WHERE note_id NOT IN (SELECT id FROM notes)"
            ).fetchall():
                found.append({"kind": f"{table}_orphan", **dict(row)})
    if table_exists(conn, "note_folders") and "folder_id" in _columns(conn, "notes"):
        for row in conn.execute(
            "SELECT id, folder_id FROM notes WHERE deleted = 0 AND folder_id IS NOT NULL "
            "AND folder_id NOT IN (SELECT id FROM note_folders)"
        ).fetchall():
            found.append({"kind": "note_missing_folder", **dict(row)})
    return found


def _smoke_check(conn: sqlite3.Connection) -> None:
    """The queries Projects, Tasks and Files run over notes must still work."""

    conn.execute("PRAGMA integrity_check").fetchall()
    if table_exists(conn, "workspace_projects") and table_exists(conn, "workspace_project_notes"):
        conn.execute(
            "SELECT p.id, (SELECT COUNT(*) FROM workspace_project_notes pn "
            "JOIN notes n ON n.id = pn.note_id WHERE pn.project_id = p.id AND n.deleted = 0) "
            "FROM workspace_projects p"
        ).fetchall()
    if table_exists(conn, "workspace_tasks") and table_exists(conn, "workspace_task_notes"):
        conn.execute(
            "SELECT t.id, (SELECT COUNT(*) FROM workspace_task_notes tn "
            "JOIN notes n ON n.id = tn.note_id WHERE tn.task_id = t.id AND n.deleted = 0) "
            "FROM workspace_tasks t"
        ).fetchall()
    if table_exists(conn, "workspace_file_links"):
        conn.execute(
            "SELECT l.id FROM workspace_file_links l JOIN notes n ON n.id = l.target_id "
            "WHERE l.link_type = 'note'"
        ).fetchall()


def _relink(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Bring back the backup's links into Notes wherever both ends still exist.

    Links live in other features' tables, which a restore does not replace. Without
    this, restoring a safety backup would bring a note back but not the projects,
    tasks and files it was attached to.
    """

    relinked: dict[str, list[dict]] = {}
    pairs = (
        ("project_links", "workspace_project_notes", "project_id", "workspace_projects"),
        ("task_links", "workspace_task_notes", "task_id", "workspace_tasks"),
    )
    for label, table, owner, owner_table in pairs:
        if not (
            table_exists(conn, table)
            and table_exists(conn, table, "b")
            and table_exists(conn, owner_table)
        ):
            continue
        rows = conn.execute(
            f"""
            SELECT x.{owner} AS {owner}, x.note_id AS note_id, x.created_at AS created_at
            FROM b.{table} x
            WHERE x.note_id IN (SELECT id FROM main.notes)
            AND x.{owner} IN (SELECT id FROM main.{owner_table})
            AND NOT EXISTS (
                SELECT 1 FROM main.{table} m WHERE m.{owner} = x.{owner} AND m.note_id = x.note_id
            )
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                f"INSERT INTO main.{table}({owner}, note_id, created_at) VALUES (?, ?, ?)",
                (row[owner], row["note_id"], row["created_at"]),
            )
        if rows:
            relinked[label] = [dict(row) for row in rows]
    if (
        table_exists(conn, "workspace_file_links")
        and table_exists(conn, "workspace_file_links", "b")
        and table_exists(conn, "workspace_files")
    ):
        rows = conn.execute(
            """
            SELECT x.id, x.file_id, x.target_id, x.title, x.metadata_json, x.created_at
            FROM b.workspace_file_links x
            WHERE x.link_type = 'note'
            AND x.target_id IN (SELECT id FROM main.notes)
            AND x.file_id IN (SELECT id FROM main.workspace_files)
            AND NOT EXISTS (
                SELECT 1 FROM main.workspace_file_links m
                WHERE m.file_id = x.file_id AND m.link_type = 'note' AND m.target_id = x.target_id
            )
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO main.workspace_file_links"
                "(id, file_id, link_type, target_id, title, metadata_json, created_at) "
                "VALUES (?, ?, 'note', ?, ?, ?, ?)",
                (
                    row["id"],
                    row["file_id"],
                    row["target_id"],
                    row["title"],
                    row["metadata_json"],
                    row["created_at"],
                ),
            )
        if rows:
            relinked["file_links"] = [dict(row) for row in rows]
    if table_exists(conn, "workspace_artifacts") and table_exists(conn, "workspace_artifacts", "b"):
        rows = conn.execute(
            """
            SELECT m.id AS id, x.note_id AS note_id
            FROM main.workspace_artifacts m JOIN b.workspace_artifacts x ON x.id = m.id
            WHERE m.note_id IS NULL AND x.note_id IN (SELECT id FROM main.notes)
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE main.workspace_artifacts SET note_id = ? WHERE id = ?",
                (row["note_id"], row["id"]),
            )
        if rows:
            relinked["artifact_note_refs"] = [dict(row) for row in rows]
    return relinked


def verify_backup(backup_path: Path) -> dict | None:
    """Check a backup against its manifest (when there is one) and its own integrity."""

    if not backup_path.is_file():
        raise NotesRestoreError(f"Backup not found: {backup_path}")
    manifest_path = backup_path.with_name(f"{backup_path.stem}.manifest.json")
    manifest = None
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("sha256") != file_sha256(backup_path):
            raise NotesRestoreError("The backup does not match its manifest.")
    check = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    try:
        integrity = [row[0] for row in check.execute("PRAGMA integrity_check")]
    finally:
        check.close()
    if integrity != ["ok"]:
        raise NotesRestoreError("The backup failed its integrity check.")
    return manifest


def backend_is_using(data_dir: Path, port: int = 8000) -> bool:
    """Whether a Neo backend on ``port`` serves this data directory right now."""

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as reply:
            payload = json.loads(reply.read() or b"{}")
    except Exception:  # noqa: BLE001 - nothing listening, or not Neo
        return False
    reported = payload.get("data_dir")
    return bool(reported) and Path(reported).resolve() == data_dir.resolve()


def restore(
    db_path: str | Path,
    backup_path: str | Path,
    *,
    confirm: Callable[[RestoreReport], bool] = lambda report: True,
) -> RestoreReport:
    """Replace the notes tables of ``db_path`` with those in ``backup_path``.

    The caller is responsible for the backend being stopped (see ``main``).
    """

    db_path = Path(db_path).resolve()
    backup_path = Path(backup_path).resolve()
    if not db_path.is_file():
        raise NotesRestoreError(f"Database not found: {db_path}")
    verify_backup(backup_path)
    report = RestoreReport(database=str(db_path), backup=str(backup_path))

    stamp = _timestamp()
    safety = write_backup(db_path, f"notes-restore-safety-{stamp}", "before-notes-restore")
    report.safety_backup = safety["backup_path"]

    conn = connect(db_path)
    try:
        conn.execute("ATTACH DATABASE ? AS b", (f"file:{backup_path}?mode=ro",))
        report.fk_before = _fk_violations(conn)
        live_ids = _note_ids(conn, "main")
        backup_ids = _note_ids(conn, "b")
        report.lost_note_ids = sorted(live_ids - backup_ids)
        if live_ids & backup_ids and table_exists(conn, "notes", "b"):
            backup_stamp = {
                row[0]: row[1] for row in conn.execute("SELECT id, updated_at FROM b.notes")
            }
            for row in conn.execute("SELECT id, updated_at FROM main.notes"):
                if row[0] in backup_stamp and backup_stamp[row[0]] != row[1]:
                    report.changed_note_ids.append(row[0])
        if not confirm(report):
            raise NotesRestoreError("Restore cancelled.")

        # SQLite's own procedure for rebuilding tables that others point at:
        # enforcement off for the swap (it cannot be changed inside a
        # transaction), then the real state checked with foreign_key_check before
        # committing. A deferred-constraint counter would misjudge a parent table
        # that is dropped and recreated; the explicit check reads what is there.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        try:
            backup_tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM b.sqlite_master WHERE type = 'table' AND name IN "
                    f"({','.join('?' * len(NOTES_OWNED_TABLES))})",
                    NOTES_OWNED_TABLES,
                )
            ]
            for table in reversed(NOTES_OWNED_TABLES):
                conn.execute(f"DROP TABLE IF EXISTS main.{table}")
            for table in NOTES_OWNED_TABLES:
                if table not in backup_tables:
                    continue
                create_sql = conn.execute(
                    "SELECT sql FROM b.sqlite_master WHERE type = 'table' AND name = ?", (table,)
                ).fetchone()[0]
                conn.execute(create_sql)
                conn.execute(f"INSERT INTO main.{table} SELECT * FROM b.{table}")
            for (index_sql,) in conn.execute(
                "SELECT sql FROM b.sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
                f"AND tbl_name IN ({','.join('?' * len(NOTES_OWNED_TABLES))})",
                NOTES_OWNED_TABLES,
            ).fetchall():
                conn.execute(index_sql)
            if not table_exists(conn, "notes"):
                _step_base_schema(conn)

            report.relinked = _relink(conn)

            removed: dict[str, list[dict]] = {}
            for label, table, query in _dangling_queries("SELECT id FROM main.notes"):
                if not table_exists(conn, table):
                    continue
                rows = [dict(row) for row in conn.execute(query).fetchall()]
                if not rows:
                    continue
                removed[label] = rows
                if label == "project_links":
                    conn.execute(
                        "DELETE FROM workspace_project_notes "
                        "WHERE note_id NOT IN (SELECT id FROM main.notes)"
                    )
                elif label == "task_links":
                    conn.execute(
                        "DELETE FROM workspace_task_notes "
                        "WHERE note_id NOT IN (SELECT id FROM main.notes)"
                    )
                elif label == "file_links":
                    conn.execute(
                        "DELETE FROM workspace_file_links WHERE link_type = 'note' "
                        "AND target_id NOT IN (SELECT id FROM main.notes)"
                    )
                elif label == "artifact_note_refs":
                    conn.execute(
                        "UPDATE workspace_artifacts SET note_id = NULL WHERE note_id IS NOT NULL "
                        "AND note_id NOT IN (SELECT id FROM main.notes)"
                    )
            report.removed = removed

            conn.execute(NOTE_SETTINGS_SQL)
            paused_until = (datetime.now(UTC) + GC_PAUSE_AFTER_RESTORE).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO note_settings(key, value_json, updated_at) "
                "VALUES ('gc_paused_until', ?, ?)",
                (json.dumps(paused_until), now_iso()),
            )
            report.gc_paused_until = paused_until

            inside = _fk_violations(conn)
            cross = cross_service_violations(conn)
            if inside or cross:
                raise NotesRestoreError(
                    "Restore would leave broken references: "
                    + json.dumps({"foreign_keys": inside, "cross_service": cross})[:2000]
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DETACH DATABASE b")

        report.fk_after = _fk_violations(conn)
        after_cross = cross_service_violations(conn)
        if report.fk_after or after_cross:
            raise NotesRestoreError(
                "Post-restore validation failed; restore the safety backup "
                f"{report.safety_backup}: "
                + json.dumps({"foreign_keys": report.fk_after, "cross_service": after_cross})[:2000]
            )
        _smoke_check(conn)
    finally:
        conn.close()

    report.restored = True
    report_path = backup_path.with_name(f"restore-report-{stamp}.json")
    report_path.write_text(json.dumps(report.__dict__, indent=2, default=str))
    report.report_path = str(report_path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.services.notes.migrations")
    sub = parser.add_subparsers(dest="command", required=True)
    restore_cmd = sub.add_parser("restore", help="restore the notes tables from a backup")
    target = restore_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument("--profile", help="account profile id")
    target.add_argument("--database", help="path to a neo.db")
    restore_cmd.add_argument("--backup", required=True, help="path to the backup .db file")
    restore_cmd.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    restore_cmd.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    root = _profiles_root()
    if args.profile:
        db_path = root / "accounts" / args.profile / "neo.db"
    else:
        db_path = Path(args.database)
    data_dir = root.parent
    if backend_is_using(data_dir, args.port):
        print(
            f"A Neo backend on port {args.port} is using {data_dir}. Stop it first, "
            "then run the restore again.",
            file=sys.stderr,
        )
        return 2

    def ask(report: RestoreReport) -> bool:
        print(f"Restoring notes in {report.database}")
        print(f"  from {report.backup}")
        print(f"  safety copy of the current database: {report.safety_backup}")
        print(f"  notes that will disappear: {len(report.lost_note_ids)}")
        print(
            f"  notes that will go back to their backed-up content: {len(report.changed_note_ids)}"
        )
        if args.yes:
            return True
        return input("Type 'restore' to continue: ").strip() == "restore"

    try:
        report = restore(db_path, Path(args.backup), confirm=ask)
    except NotesRestoreError as exc:
        print(f"Restore refused: {exc}", file=sys.stderr)
        return 1
    removed = {key: len(value) for key, value in report.removed.items()}
    print(f"Restored. Removed dangling references: {removed or 'none'}")
    print(f"Report: {report.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
