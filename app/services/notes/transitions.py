"""Lock, Remove Lock and re-key: one note's content changing form, recoverably.

All three are the same two-phase transition:

1. **Begin.** The note must be in Recently Deleted's opposite (not deleted)
   and at the revision the browser last saw. Any earlier unfinished
   transition of the same note is abandoned.
2. **Prepare** (in the browser). It encrypts (or decrypts) the content, every
   version, every conflict and every attachment, uploads the attachments
   here as staged files (this module records the SHA-256 of what it stored),
   downloads them again and checks them, and decrypts what it encrypted to
   check that too. Any failure is an abort: the note is untouched.
3. **Commit**, one SQLite transaction: the transition is still open, the
   note is still at its revision and not deleted (so a save that landed in
   between aborts the lock instead of being lost), every staged file it
   names exists, belongs to this transition and still has the bytes it was
   staged with, and every version and conflict is covered. Only then does the
   note change form; the files it no longer needs are queued for collection
   in the same transaction and removed after it. A retried commit returns the
   first one's result.

Nothing is deleted before the new form is committed. A transition left open
for an hour (the tab went away mid-lock) is abandoned by maintenance, and its
staged files collected.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.services.notes import store
from app.services.notes.content import Derived, derive_from_doc, derive_from_markdown

KINDS = {"lock", "unlock", "rekey"}
ABANDON_AFTER = timedelta(hours=1)
#: What a transition's staged files are made as: ciphertext, or (Remove Lock) plain.
STAGED_SOURCE = {"lock": "note_encrypted", "rekey": "note_encrypted", "unlock": "note"}


class TransitionNotFound(LookupError):
    pass


class TransitionClosed(RuntimeError):
    """The transition was abandoned (or superseded); begin again."""


class TransitionInvalid(ValueError):
    """What the commit names doesn't hold up; the note is left as it was."""


# ----------------------------------------------------------------- begin --


def begin(note_id: str, kind: str, base_revision: int) -> dict:
    if kind not in KINDS:
        raise TransitionInvalid("Unknown kind of change.")
    conn = store._connect()
    try:
        with store._tx(conn):
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise store.NoteGone(note_id)
            if row["deleted"]:
                raise store.NoteDeleted(note_id)
            if row["revision"] != base_revision:
                raise store.StaleRevision(note_id)
            encrypted = row["content_format"] == "encrypted"
            if (kind == "lock") == encrypted:
                raise TransitionInvalid(
                    "This note is already locked." if encrypted else "This note isn't locked."
                )
            for stale in conn.execute(
                "SELECT id FROM note_transitions WHERE note_id = ? AND state = 'begun'", (note_id,)
            ).fetchall():
                _abandon(conn, stale["id"])
            transition_id = store.new_id()
            now = store.now_iso()
            conn.execute(
                "INSERT INTO note_transitions(id, note_id, kind, base_revision, state, "
                "staged_files_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'begun', '[]', ?, ?)",
                (transition_id, note_id, kind, base_revision, now, now),
            )
        store.run_pending_operations()
        return {"id": transition_id, "kind": kind, "base_revision": base_revision, "state": "begun"}
    finally:
        conn.close()


def _open_transition(conn: sqlite3.Connection, note_id: str, transition_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM note_transitions WHERE id = ? AND note_id = ?", (transition_id, note_id)
    ).fetchone()
    if row is None:
        raise TransitionNotFound(transition_id)
    return row


# ---------------------------------------------------------------- stage --


def stage_file(
    note_id: str, transition_id: str, *, content: bytes, name: str, mime_type: str | None
) -> dict:
    """Store one prepared attachment for this transition and record what it holds."""

    from app.services.files.service import WorkspaceFilesService

    conn = store._connect()
    try:
        row = _open_transition(conn, note_id, transition_id)
        if row["state"] != "begun":
            raise TransitionClosed(transition_id)
        source = STAGED_SOURCE[row["kind"]]
    finally:
        conn.close()
    encrypted = source == "note_encrypted"
    # Ciphertext says nothing about itself: no name, no type (both are inside the note).
    item = WorkspaceFilesService().import_bytes(
        original_filename="encrypted" if encrypted else (name or "attachment"),
        content=content,
        mime_type="application/octet-stream" if encrypted else mime_type,
        deduplicate=False,
        source_type=source,
    )
    digest = hashlib.sha256(content).hexdigest()
    conn = store._connect()
    try:
        with store._tx(conn):
            row = _open_transition(conn, note_id, transition_id)
            if row["state"] != "begun":
                # Abandoned while uploading: the file is nobody's, and is collected.
                store._enqueue(conn, "collect_files", {"file_ids": [item["id"]]})
                raise TransitionClosed(transition_id)
            staged = json.loads(row["staged_files_json"])
            staged.append({"file_id": item["id"], "sha256": digest, "size": len(content)})
            conn.execute(
                "UPDATE note_transitions SET staged_files_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(staged), store.now_iso(), transition_id),
            )
            # Held by the transition, so collection leaves it alone until commit or abort.
            conn.execute(
                "INSERT OR IGNORE INTO note_attachment_refs(file_id, note_id, source, ref_id, "
                "created_at) VALUES (?, ?, 'transition', ?, ?)",
                (item["id"], note_id, transition_id, store.now_iso()),
            )
    finally:
        conn.close()
    store.run_pending_operations()
    return {"file_id": item["id"], "sha256": digest, "size": len(content)}


# ---------------------------------------------------------------- abort --


def _abandon(conn: sqlite3.Connection, transition_id: str) -> None:
    """Inside a transaction: close it and queue its staged files for collection."""

    row = conn.execute("SELECT * FROM note_transitions WHERE id = ?", (transition_id,)).fetchone()
    if row is None or row["state"] != "begun":
        return
    staged = [item["file_id"] for item in json.loads(row["staged_files_json"])]
    conn.execute(
        "DELETE FROM note_attachment_refs WHERE source = 'transition' AND ref_id = ?",
        (transition_id,),
    )
    conn.execute(
        "UPDATE note_transitions SET state = 'aborted', updated_at = ? WHERE id = ?",
        (store.now_iso(), transition_id),
    )
    if staged:
        store._enqueue(conn, "collect_files", {"file_ids": staged})


def abort(note_id: str, transition_id: str) -> dict:
    conn = store._connect()
    try:
        with store._tx(conn):
            row = _open_transition(conn, note_id, transition_id)
            if row["state"] == "committed":
                raise TransitionClosed(transition_id)
            _abandon(conn, transition_id)
    finally:
        conn.close()
    store.run_pending_operations()
    return {"id": transition_id, "state": "aborted"}


def sweep(*, now: datetime | None = None) -> int:
    """Abandon transitions their tab never finished."""

    now = now or datetime.now(UTC)
    conn = store._connect()
    try:
        if not store._has_table(conn, "note_transitions"):
            return 0
        cutoff = (now - ABANDON_AFTER).isoformat()
        with store._tx(conn):
            stale = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM note_transitions WHERE state = 'begun' AND created_at < ?",
                    (cutoff,),
                )
            ]
            for transition_id in stale:
                _abandon(conn, transition_id)
    finally:
        conn.close()
    if stale:
        store.run_pending_operations()
    return len(stale)


# --------------------------------------------------------------- commit --


def _check_cipher(form: dict) -> None:
    from app.services.notes.service import _check_cipher as check

    try:
        check(form.get("cipher"), form.get("lock_key_version"))
    except ValueError as exc:
        raise TransitionInvalid(str(exc)) from exc


def _plain_files(form: dict) -> tuple[str, ...]:
    if form.get("format") == "markdown":
        return derive_from_markdown(form.get("body") or "").attachment_file_ids
    return derive_from_doc(form.get("doc"), form.get("body") or "").attachment_file_ids


def _check_form(kind: str, form: dict, *, what: str) -> tuple[str, ...]:
    """Validate one piece of new content; return the files it refers to."""

    from app.services.notes.service import NotesValidationError, _check_attachment_ids, _check_doc

    if kind in ("lock", "rekey"):
        _check_cipher(form)
        if form.get("body") or form.get("doc") or form.get("tags") or form.get("summary"):
            raise TransitionInvalid(f"The {what} must be encrypted.")
        try:
            return tuple(_check_attachment_ids(list(form.get("attachment_ids") or [])))
        except NotesValidationError as exc:
            raise TransitionInvalid(str(exc)) from exc
    if form.get("cipher"):
        raise TransitionInvalid(f"The {what} must not be encrypted.")
    if (form.get("format") or "rich") == "rich":
        if not isinstance(form.get("doc"), dict):
            raise TransitionInvalid(f"The {what} needs its document.")
        try:
            _check_doc(form["doc"])
        except NotesValidationError as exc:
            raise TransitionInvalid(str(exc)) from exc
    elif form.get("format") != "markdown":
        raise TransitionInvalid(f"The {what} has an unknown format.")
    return _plain_files(form)


def _note_files(conn: sqlite3.Connection, note_id: str) -> set[str]:
    return {
        r["file_id"]
        for r in conn.execute(
            "SELECT file_id FROM note_attachment_refs WHERE note_id = ? "
            "AND source IN ('doc', 'version', 'conflict')",
            (note_id,),
        )
    }


def _check_staged(conn: sqlite3.Connection, transition: sqlite3.Row, used: set[str]) -> None:
    """Every file the new content names was staged by this transition, intact."""

    staged = {item["file_id"]: item for item in json.loads(transition["staged_files_json"])}
    source = STAGED_SOURCE[transition["kind"]]
    for file_id in used:
        item = staged.get(file_id)
        if item is None:
            raise TransitionInvalid("An attachment wasn't prepared for this change.")
        row = conn.execute(
            "SELECT source_type, sha256, storage_path FROM workspace_files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or row["source_type"] != source or row["sha256"] != item["sha256"]:
            raise TransitionInvalid("A prepared attachment is missing or changed.")
        path = Path(row["storage_path"]).expanduser()
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise TransitionInvalid("A prepared attachment is missing or changed.")


def _columns_for(kind: str, form: dict) -> tuple[dict[str, Any], Derived]:
    """The content columns a note, version or conflict takes in its new form."""

    if kind in ("lock", "rekey"):
        return (
            {
                "content_format": "encrypted",
                "body": "",
                "doc_json": None,
                "summary": None,
                "cipher_json": json.dumps(form["cipher"]),
                "lock_key_version": form["lock_key_version"],
            },
            Derived(),
        )
    markdown = form.get("format") == "markdown"
    body = form.get("body") or ""
    derived = derive_from_markdown(body) if markdown else derive_from_doc(form["doc"], body)
    return (
        {
            "content_format": "markdown" if markdown else "rich",
            "body": body,
            "doc_json": None if markdown else json.dumps(form["doc"]),
            "summary": form.get("summary"),
            "cipher_json": None,
            "lock_key_version": None,
        },
        derived,
    )


def commit(note_id: str, transition_id: str, payload: dict) -> dict:
    """Apply the prepared change in one transaction, or change nothing.

    ``payload`` has ``content`` (the note's new form), and ``versions`` and
    ``conflicts``: each a map from id to its new form, covering all of them.
    A commit that doesn't hold up abandons the transition (the note is as it was).
    """

    try:
        return _commit(note_id, transition_id, payload)
    except TransitionInvalid:
        try:
            abort(note_id, transition_id)
        except (TransitionClosed, TransitionNotFound):
            pass
        raise


def _commit(note_id: str, transition_id: str, payload: dict) -> dict:
    from app.services.notes.service import _clean_title, _normalize_tags

    conn = store._connect()
    try:
        failure: Exception | None = None
        with store._tx(conn):
            transition = _open_transition(conn, note_id, transition_id)
            if transition["state"] == "committed":
                return {
                    "transition": transition_id,
                    "replayed": True,
                    **json.loads(transition["result_json"]),
                }
            if transition["state"] != "begun":
                raise TransitionClosed(transition_id)
            kind = transition["kind"]
            note = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if note is None:
                raise store.NoteGone(note_id)
            if note["deleted"] or note["revision"] != transition["base_revision"]:
                # Someone saved (or deleted) in between: this change is void.
                _abandon(conn, transition_id)
                failure = (
                    store.NoteDeleted(note_id) if note["deleted"] else store.StaleRevision(note_id)
                )
            else:
                versions = {
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM note_versions WHERE note_id = ?", (note_id,)
                    )
                }
                conflicts = {
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM note_conflicts WHERE note_id = ?", (note_id,)
                    )
                }
                given_versions = payload.get("versions") or {}
                given_conflicts = payload.get("conflicts") or {}
                if set(given_versions) != versions or set(given_conflicts) != conflicts:
                    raise TransitionInvalid(
                        "Every version and conflict of the note must be included."
                    )
                content = payload.get("content") or {}
                used: dict[tuple[str, str], tuple[str, ...]] = {
                    ("doc", note_id): _check_form(kind, content, what="note")
                }
                for version_id, form in given_versions.items():
                    used[("version", version_id)] = _check_form(kind, form, what="version")
                for conflict_id, form in given_conflicts.items():
                    used[("conflict", conflict_id)] = _check_form(kind, form, what="conflict")
                all_used = {file_id for ids in used.values() for file_id in ids}
                _check_staged(conn, transition, all_used)
                before = _note_files(conn, note_id)

                columns, derived = _columns_for(kind, content)
                title = _clean_title(content.get("title")) or note["title"]
                assignments = ", ".join(
                    f"{column} = ?" for column in (*columns, *derived.columns())
                )
                cursor = conn.execute(
                    f"UPDATE notes SET title = ?, {assignments}, revision = revision + 1 "
                    "WHERE id = ? AND revision = ? AND deleted = 0",
                    (
                        title,
                        *columns.values(),
                        *derived.columns().values(),
                        note_id,
                        note["revision"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("compare_and_swap_lost_inside_transaction")
                tags = (
                    [] if kind in ("lock", "rekey") else _normalize_tags(content.get("tags") or [])
                )
                store._replace_tags(conn, note_id, tags)

                for table, given in (
                    ("note_versions", given_versions),
                    ("note_conflicts", given_conflicts),
                ):
                    for row_id, form in given.items():
                        values, _ = _columns_for(kind, form)
                        row_tags = (
                            []
                            if kind in ("lock", "rekey")
                            else _normalize_tags(form.get("tags") or [])
                        )
                        conn.execute(
                            f"UPDATE {table} SET title = coalesce(?, title), "
                            "content_format = ?, body = ?, doc_json = ?, summary = ?, "
                            "cipher_json = ?, lock_key_version = ?, tags_json = ? "
                            "WHERE id = ? AND note_id = ?",
                            (
                                _clean_title(form.get("title")) or None,
                                *values.values(),
                                json.dumps(row_tags),
                                row_id,
                                note_id,
                            ),
                        )
                for (source, ref_id), file_ids in used.items():
                    store._replace_refs(conn, note_id, source, ref_id, list(file_ids))
                for conflict_id in given_conflicts:
                    conn.execute(
                        "UPDATE note_conflicts SET attachment_ids_json = ? WHERE id = ?",
                        (json.dumps(list(used[("conflict", conflict_id)])), conflict_id),
                    )
                conn.execute(
                    "DELETE FROM note_attachment_refs WHERE source = 'transition' AND ref_id = ?",
                    (transition_id,),
                )
                # The old form's files, and staged ones the new form didn't use, go
                # (each only if nothing else refers to it).
                staged = {item["file_id"] for item in json.loads(transition["staged_files_json"])}
                retired = sorted((before | staged) - all_used)
                if retired:
                    store._enqueue(conn, "collect_files", {"file_ids": retired})
                result = {
                    "revision": note["revision"] + 1,
                    "content_format": columns["content_format"],
                    "retired_files": len(retired),
                }
                conn.execute(
                    "UPDATE note_transitions SET state = 'committed', result_json = ?, "
                    "updated_at = ? "
                    "WHERE id = ?",
                    (json.dumps(result), store.now_iso(), transition_id),
                )
    finally:
        conn.close()
    store.run_pending_operations()
    if failure is not None:
        raise failure
    _checkpoint()
    return {"transition": transition_id, "replayed": False, **result}


def _checkpoint() -> None:
    """Fold the write-ahead log into the database, so the old form isn't left in it."""

    conn = store._connect()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
