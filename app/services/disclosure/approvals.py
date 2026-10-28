"""One-use, profile-bound approvals for an exact external-agent prompt.

The approved text is stored in PRIVATE profile storage. A worker re-reads the
source and rebuilds the prompt before consuming an approval, so an edit between
preview and execution fails closed. No model-facing tool can create or approve a
record; the HTTP route requires the user's profile session and CSRF token.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.core.config import active_profile_storage_dir, get_settings
from app.services.disclosure import GrantStore, Label, Labeled, content_hash, join, public
from app.services.external_agents.disclosure import Payload, build_payload

PREVIEW_LIFETIME_SECONDS = 600
MAX_SOURCE_CHARS = 12_000
AUDIT_INSERT = (
    "INSERT INTO disclosure_audit"
    "(approval_id,event,profile_id,engine,payload_hash,created_at) "
    "VALUES (?,?,?,?,?,?)"
)


class DisclosureApprovalError(ValueError):
    """A preview, approval, or source is no longer valid."""


@dataclass(frozen=True)
class ApprovedPayload:
    approval_id: str
    payload: Payload


def _profile_file(profile_id: str) -> Path:
    storage = active_profile_storage_dir.get()
    if not storage or Path(storage).name != profile_id:
        raise DisclosureApprovalError("profile_scope_required")
    path = Path(storage) / "disclosure-grants.db"
    if path.is_symlink() or Path(storage).is_symlink():
        raise DisclosureApprovalError("unsafe_grant_store")
    return path


def _connect(profile_id: str) -> sqlite3.Connection:
    path = _profile_file(profile_id)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    os.chmod(path, 0o600)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS disclosure_previews (
            id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            request_id TEXT NOT NULL UNIQUE,
            engine TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            source_version TEXT NOT NULL,
            objective_hash TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            payload_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            consumed_session_id TEXT
        );
        CREATE TABLE IF NOT EXISTS disclosure_csrf (
            session_hash TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            token TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS disclosure_audit (
            id INTEGER PRIMARY KEY,
            approval_id TEXT NOT NULL,
            event TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            engine TEXT NOT NULL,
            payload_hash TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(disclosure_previews)")}
    if "source_kind" not in columns:
        # Preserve previews written by the first local implementation. The
        # table swap is one SQLite transaction; a crash leaves either schema.
        conn.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(disclosure_previews)")}
        if "source_kind" not in columns:
            conn.execute(
                "CREATE TABLE disclosure_previews_v2 ("
                "id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, chat_id INTEGER NOT NULL, "
                "request_id TEXT NOT NULL UNIQUE, engine TEXT NOT NULL, "
                "source_kind TEXT NOT NULL, source_id TEXT NOT NULL, "
                "source_version TEXT NOT NULL, objective_hash TEXT NOT NULL, "
                "payload_hash TEXT NOT NULL, payload_text TEXT NOT NULL, "
                "status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, "
                "consumed_session_id TEXT)"
            )
            conn.execute(
                "INSERT INTO disclosure_previews_v2 SELECT id, profile_id, chat_id, "
                "request_id, engine, 'chat_message', CAST(source_message_id AS TEXT), "
                "source_version, objective_hash, payload_hash, payload_text, status, "
                "created_at, expires_at, consumed_session_id FROM disclosure_previews"
            )
            conn.execute("DROP TABLE disclosure_previews")
            conn.execute("ALTER TABLE disclosure_previews_v2 RENAME TO disclosure_previews")
        conn.commit()
    return conn


def _source(profile_id: str, chat_id: int, source_kind: str, source_id: str) -> tuple[str, str]:
    """Load one immutable-versioned source in the active profile."""

    if source_kind == "note":
        from app.services.notes.service import NotesService

        note = NotesService().get_note(source_id)
        if note is None or note.deleted or note.content_format != "markdown":
            raise DisclosureApprovalError("note_unavailable_or_locked")
        text = f"Note {note.title}:\n{note.body}".strip()
        if not text or len(text) > MAX_SOURCE_CHARS:
            raise DisclosureApprovalError("note_empty_or_too_large")
        label = _stored_note_label(profile_id, source_id)
        return text, content_hash({
            "title": note.title, "body": note.body,
            "revision": note.revision, "updated_at": note.updated_at,
            "source_label": {
                "state": label.state, "tags": sorted(label.tags),
                "basis": label.basis, "reason": label.reason,
            },
        })
    if source_kind != "chat_message":
        raise DisclosureApprovalError("unsupported_source_kind")
    try:
        message_id = int(source_id)
    except ValueError as exc:
        raise DisclosureApprovalError("message_not_found") from exc

    db_url = get_settings().database_url
    if not db_url.startswith("sqlite:///"):
        raise DisclosureApprovalError("unsupported_profile_database")
    db_path = Path(db_url.removeprefix("sqlite:///"))
    if db_path.parent != Path(active_profile_storage_dir.get() or ""):
        raise DisclosureApprovalError("profile_database_mismatch")
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT id, role, content, metadata_json FROM chat_messages "
            "WHERE chat_id = ? ORDER BY created_at, id",
            (chat_id,),
        ).fetchall()
    target = next((row for row in rows if row[0] == message_id), None)
    if target is None or target[1] not in {"user", "assistant"}:
        raise DisclosureApprovalError("message_not_found")
    # A deletion of remembered information redacts both the confirming answer
    # and the user request immediately before it from future model context.
    redacted: set[int] = set()
    for index, row in enumerate(rows):
        if row[1] != "assistant":
            continue
        try:
            metadata = json.loads(row[3] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        extraction = metadata.get("memory_extraction") if isinstance(metadata, dict) else None
        if not (isinstance(extraction, dict) and extraction.get("forgotten_memories")):
            continue
        redacted.add(row[0])
        for earlier in reversed(rows[:index]):
            if earlier[1] == "user":
                redacted.add(earlier[0])
                break
    if message_id in redacted:
        raise DisclosureApprovalError("forgotten_message_cannot_be_shared")
    text = target[2].strip()
    if not text or len(text) > MAX_SOURCE_CHARS:
        raise DisclosureApprovalError("message_empty_or_too_large")
    source_version = content_hash({"role": target[1], "content": text, "metadata": target[3]})
    return f"Earlier {target[1]} message:\n{text}", source_version


def _stored_note_label(profile_id: str, source_id: str) -> Label:
    from app.services.disclosure.artifacts import ArtifactLabels

    storage = _profile_file(profile_id).parent
    return ArtifactLabels(storage / "artifact-labels.json").get(f"note:{source_id}")


def _payload(
    *, profile_id: str, engine: str, thread_id: str, objective: str, source_id: str,
    source_text: str, source_kind: str = "chat_message",
) -> Payload:
    source_tag = f"note:{source_id}" if source_kind == "note" else f"chat:{source_id}"
    source_label = Label.private(source_tag)
    if source_kind == "note":
        inherited = _stored_note_label(profile_id, source_id)
        if inherited.reason != "no_artifact_record":
            source_label = join(source_label, inherited)
    granted = Labeled(source_text, source_label)
    grants = GrantStore.in_memory()
    grants.create(
        profile_id=profile_id,
        engines=frozenset({engine}),
        thread_scope=thread_id,
        tags=granted.label.tags,
        content_hash=content_hash(granted),
        sink="external_agent",
        ttl_seconds=PREVIEW_LIFETIME_SECONDS,
    )
    return build_payload(
        objective=Labeled(objective, public(f"user_input:{thread_id}")),
        engine=engine,
        thread_id=thread_id,
        profile_id=profile_id,
        granted=[granted],
        grants=grants,
    )


def csrf_token(profile_id: str, session_cookie: str) -> str:
    if not session_cookie:
        raise DisclosureApprovalError("session_required")
    session_hash = hashlib.sha256(session_cookie.encode()).hexdigest()
    with closing(_connect(profile_id)) as conn, conn:
        row = conn.execute(
            "SELECT token FROM disclosure_csrf WHERE session_hash=? AND profile_id=?",
            (session_hash, profile_id),
        ).fetchone()
        if row:
            return str(row[0])
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO disclosure_csrf(session_hash, profile_id, token, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_hash, profile_id, token, time.time()),
        )
        return token


def check_csrf(profile_id: str, session_cookie: str, supplied: str) -> None:
    if not supplied or not secrets.compare_digest(
        csrf_token(profile_id, session_cookie), supplied
    ):
        raise DisclosureApprovalError("invalid_csrf_token")


def preview(
    *, profile_id: str, chat_id: int, request_id: str, engine: str,
    objective: str, source_message_id: int | None = None,
    source_note_id: str | None = None,
) -> dict:
    if not request_id or not objective.strip() or len(objective) > 20_000:
        raise DisclosureApprovalError("invalid_objective_or_request")
    if (source_message_id is None) == (source_note_id is None):
        raise DisclosureApprovalError("choose_one_source")
    source_kind = "note" if source_note_id is not None else "chat_message"
    source_id = str(source_note_id if source_note_id is not None else source_message_id)
    source_text, version = _source(profile_id, chat_id, source_kind, source_id)
    payload = _payload(
        profile_id=profile_id, engine=engine, thread_id=request_id,
        objective=objective.strip(), source_id=source_id, source_kind=source_kind,
        source_text=source_text,
    )
    if len(payload.segments) != 2 or payload.withheld:
        raise DisclosureApprovalError("payload_not_authorized")
    now = time.time()
    approval_id = uuid.uuid4().hex
    with closing(_connect(profile_id)) as conn, conn:
        existing = conn.execute(
            "SELECT * FROM disclosure_previews WHERE request_id=?", (request_id,)
        ).fetchone()
        if existing:
            if (
                existing["profile_id"] != profile_id or existing["chat_id"] != chat_id
                or existing["engine"] != engine
                or existing["source_kind"] != source_kind
                or existing["source_id"] != source_id
                or existing["source_version"] != version
                or existing["payload_hash"] != payload.content_hash
                or existing["status"] not in {"preview", "approved"}
                or existing["expires_at"] <= now
            ):
                raise DisclosureApprovalError("request_id_already_used")
            approval_id = existing["id"]
        else:
            conn.execute(
                "INSERT INTO disclosure_previews VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (approval_id, profile_id, chat_id, request_id, engine, source_kind, source_id,
                 version, content_hash(objective.strip()), payload.content_hash,
                 payload.text, "preview", now, now + PREVIEW_LIFETIME_SECONDS, None),
            )
            conn.execute(
                AUDIT_INSERT,
                (approval_id, "previewed", profile_id, engine, payload.content_hash, now),
            )
    return {
        "id": approval_id, "text": payload.text, "payload_hash": payload.content_hash,
        "expires_at": now + PREVIEW_LIFETIME_SECONDS,
        "source": {"kind": source_kind, "id": source_id},
    }


def decide(profile_id: str, approval_id: str, *, approve: bool) -> None:
    now = time.time()
    with closing(_connect(profile_id)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM disclosure_previews WHERE id=? AND profile_id=?",
            (approval_id, profile_id),
        ).fetchone()
        if row is None or row["expires_at"] <= now or row["status"] != "preview":
            raise DisclosureApprovalError("preview_unavailable")
        status = "approved" if approve else "revoked"
        conn.execute("UPDATE disclosure_previews SET status=? WHERE id=?", (status, approval_id))
        conn.execute(
            AUDIT_INSERT,
            (approval_id, status, profile_id, row["engine"], row["payload_hash"], now),
        )


def revoke(profile_id: str, approval_id: str) -> None:
    now = time.time()
    with closing(_connect(profile_id)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM disclosure_previews WHERE id=? AND profile_id=?",
            (approval_id, profile_id),
        ).fetchone()
        if row is None:
            raise DisclosureApprovalError("approval_not_found")
        conn.execute(
            "UPDATE disclosure_previews SET status='revoked' WHERE id=?", (approval_id,)
        )
        conn.execute(
            AUDIT_INSERT,
            (approval_id, "revoked", profile_id, row["engine"], row["payload_hash"], now),
        )


def list_for_chat(profile_id: str, chat_id: int) -> list[dict]:
    """Show revocable grants without returning their private prompt text."""

    with closing(_connect(profile_id)) as conn:
        rows = conn.execute(
            "SELECT id, engine, source_kind, source_id, status, expires_at, consumed_session_id "
            "FROM disclosure_previews WHERE profile_id=? AND chat_id=? "
            "ORDER BY created_at DESC LIMIT 100",
            (profile_id, chat_id),
        ).fetchall()
    now = time.time()
    return [
        {
            "id": row["id"], "engine": row["engine"],
            "source_kind": row["source_kind"], "source_id": row["source_id"],
            "status": "expired" if row["expires_at"] <= now
            and row["status"] in {"preview", "approved"} else row["status"],
            "expires_at": row["expires_at"],
            "consumed_session_id": row["consumed_session_id"],
        }
        for row in rows
    ]


def consume_for_run(
    *, profile_id: str, chat_id: int | None, request_id: str | None,
    session_id: str, engine: str, objective: str,
) -> ApprovedPayload | None:
    if not request_id:
        return None
    now = time.time()
    with closing(_connect(profile_id)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM disclosure_previews WHERE request_id=? AND profile_id=?",
            (request_id, profile_id),
        ).fetchone()
        if row is None:
            return None
        if (
            row["status"] != "approved" or row["expires_at"] <= now
            or row["chat_id"] != chat_id or row["engine"] != engine
            or row["objective_hash"] != content_hash(objective.strip())
        ):
            raise DisclosureApprovalError("approval_does_not_cover_run")
        source_text, version = _source(
            profile_id, row["chat_id"], row["source_kind"], row["source_id"]
        )
        if version != row["source_version"]:
            raise DisclosureApprovalError("source_changed_preview_again")
        payload = _payload(
            profile_id=profile_id, engine=engine, thread_id=session_id,
            objective=objective.strip(), source_id=row["source_id"],
            source_kind=row["source_kind"],
            source_text=source_text,
        )
        if payload.content_hash != row["payload_hash"] or payload.text != row["payload_text"]:
            raise DisclosureApprovalError("payload_changed_preview_again")
        conn.execute(
            "UPDATE disclosure_previews SET status='consumed', consumed_session_id=? WHERE id=?",
            (session_id, row["id"]),
        )
        conn.execute(
            AUDIT_INSERT,
            (row["id"], "consumed", profile_id, engine, row["payload_hash"], now),
        )
        return ApprovedPayload(approval_id=row["id"], payload=payload)


def still_active(profile_id: str, approval_id: str, session_id: str) -> bool:
    with closing(_connect(profile_id)) as conn:
        row = conn.execute(
            "SELECT status, expires_at, consumed_session_id FROM disclosure_previews "
            "WHERE id=? AND profile_id=?",
            (approval_id, profile_id),
        ).fetchone()
    return bool(
        row and row["status"] == "consumed" and row["consumed_session_id"] == session_id
        and row["expires_at"] > time.time()
    )
