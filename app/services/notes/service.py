from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

import app.services.notes.store as store
from app.services.notes.content import Derived, derive_from_doc, derive_from_markdown
from app.services.notes.smart import SmartRules
from app.services.notes.types import (
    ConflictResolution,
    Note,
    NoteConflict,
    NoteContentWrite,
    NoteCreate,
    NoteFolder,
    NoteFolderCreate,
    NoteFolderUpdate,
    NoteListItem,
    NoteSettings,
    NoteSettingsUpdate,
    NoteSidebar,
    NoteTag,
    NoteUpdate,
    NoteVersion,
    NoteVersionSummary,
    SmartFolder,
    SmartFolderCreate,
    SmartFolderUpdate,
)
from app.services.research.types import JobStatus, ResearchJob

MAX_TITLE_LENGTH = 200
MAX_TAG_LENGTH = 40
MAX_TAGS = 100
MAX_BODY_LENGTH = 1_000_000
MAX_DOC_BYTES = 4_000_000
MAX_CIPHER_BYTES = 6_000_000
UNTITLED = "New Note"
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")


class NotesValidationError(ValueError):
    pass


#: Pictures every browser Neo supports can show inline, by their first bytes.
_IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_image_type(content: bytes) -> str | None:
    """The image type a file's bytes say it is, or None for anything else."""

    for signature, mime in _IMAGE_SIGNATURES:
        if content.startswith(signature):
            return mime
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if content[4:8] == b"ftyp" and content[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


class NotesService:
    # ------------------------------------------------------------- create --

    def create_note(self, payload: NoteCreate) -> Note:
        return self.create(payload)[0]

    def create(self, payload: NoteCreate) -> tuple[Note, bool]:
        """Create a note; also says whether a create with this write id already ran."""

        doc = payload.doc
        if doc is not None:
            _check_doc(doc)
            body = _check_body(payload.body)
            title = _clean_title(payload.title) or UNTITLED
            derived = derive_from_doc(doc, body)
            content_format = "rich"
        else:
            title, body = _clean_title_body(payload.title, payload.body)
            derived = derive_from_markdown(body)
            content_format = "markdown"
        if payload.folder_id and store.get_folder(payload.folder_id) is None:
            raise NotesValidationError("That folder no longer exists.")
        now = store.now_iso()
        note = {
            "id": str(uuid.uuid4()),
            "title": title,
            "body": body,
            "summary": _clean_optional_text(payload.summary),
            "tags": _normalize_tags(payload.tags),
            "source_type": payload.source_type or "manual",
            "source_id": _clean_optional_text(payload.source_id),
            "source_url": _clean_optional_text(payload.source_url),
            "source_title": _clean_optional_text(payload.source_title),
            "source_metadata": payload.source_metadata or {},
            "created_at": now,
            "updated_at": now,
            "content_format": content_format,
            "doc": doc,
            "folder_id": payload.folder_id,
            "write_id": payload.write_id,
            "derived": derived,
        }
        created, replayed = store.insert_note(note)
        return Note(**created), replayed

    # -------------------------------------------------------------- reads --

    def get_note(self, note_id: str, *, include_deleted: bool = False) -> Note | None:
        note = store.get_note(note_id, include_deleted=include_deleted)
        return Note(**note) if note else None

    def find_by_source(self, source_type: str, source_id: str) -> Note | None:
        note = store.find_note_by_source(source_type, source_id)
        return Note(**note) if note else None

    def list_notes(
        self,
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
    ) -> tuple[list[NoteListItem], int]:
        cleaned_q = q.strip() if q and q.strip() else None
        cleaned_tag = _normalize_tag(tag) if tag and tag.strip() else None
        cleaned_tags = [_normalize_tag(item) for item in tags or [] if item and item.strip()]
        notes, total = store.list_notes(
            q=cleaned_q,
            tag=cleaned_tag,
            tags=cleaned_tags,
            tags_mode="all" if tags_mode == "all" else "any",
            include_archived=include_archived,
            pinned_first=pinned_first,
            view=view,
            folder_id=folder_id,
            smart_id=smart_id,
            tz_offset=tz_offset,
            sort=sort,
            order=order,
            include_body=include_body,
            limit=max(1, min(limit, 500)),
            offset=max(0, offset),
        )
        return [NoteListItem(**note) for note in notes], total

    def list_tags(self) -> list[NoteTag]:
        return [NoteTag(**row) for row in store.list_tags()]

    def sidebar(self, *, tz_offset: int = 0) -> NoteSidebar:
        counts = store.sidebar_counts()
        folders = [
            NoteFolder(**folder, count=counts["per_folder"].get(folder["id"], 0))
            for folder in store.list_folders()
        ]
        tags = [NoteTag(**row) for row in store.list_tags(include_archived=False)]
        smart = [SmartFolder(**folder) for folder in store.list_smart_folders(tz_offset=tz_offset)]
        return NoteSidebar(folders=folders, counts=counts["counts"], tags=tags, smart_folders=smart)

    # ------------------------------------------------------- smart folders --

    @staticmethod
    def _rules(rules: dict) -> SmartRules:
        try:
            return SmartRules.model_validate(rules)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise NotesValidationError(
                f"That rule isn't valid: {first.get('msg', 'unknown')}."
            ) from exc

    def create_smart_folder(self, payload: SmartFolderCreate) -> SmartFolder:
        name = payload.name.strip()
        if not name:
            raise NotesValidationError("A smart folder needs a name.")
        folder = store.create_smart_folder(name, self._rules(payload.rules))
        return SmartFolder(**folder)

    def update_smart_folder(self, smart_id: str, payload: SmartFolderUpdate) -> SmartFolder:
        name = payload.name.strip() if payload.name is not None else None
        if name == "":
            raise NotesValidationError("A smart folder needs a name.")
        rules = self._rules(payload.rules) if payload.rules is not None else None
        return SmartFolder(**store.update_smart_folder(smart_id, name=name, rules=rules))

    def delete_smart_folder(self, smart_id: str) -> bool:
        return store.delete_smart_folder(smart_id)

    # ---------------------------------------------------------------- tags --

    def rename_tag(self, old: str, new: str) -> list[dict]:
        old_tag, new_tag = _normalize_tag(old), _normalize_tag(new)
        if not old_tag or not new_tag:
            raise NotesValidationError("A tag needs a name.")
        if old_tag == new_tag:
            return []
        if not re.search(r"[^\W\d_]", new_tag) or re.search(r"[^\w-]", new_tag):
            raise NotesValidationError(
                "A tag is letters, numbers, - and _, with at least one letter."
            )
        return store.retag(old_tag, new_tag)

    def delete_tag(self, tag: str) -> list[dict]:
        tag = _normalize_tag(tag)
        if not tag:
            raise NotesValidationError("A tag needs a name.")
        return store.retag(tag, None)

    # ------------------------------------------------------ content writes --

    def write_content(self, note_id: str, payload: NoteContentWrite) -> Note:
        title = _clean_title(payload.title) or UNTITLED
        if payload.format == "rich":
            if payload.doc is None:
                raise NotesValidationError("A rich note needs its document.")
            _check_doc(payload.doc)
            body = _check_body(payload.body)
            derived = derive_from_doc(payload.doc, body)
            write = {
                "format": "rich",
                "write_id": payload.write_id,
                "base_revision": payload.base_revision,
                "title": title,
                "body": body,
                "doc": payload.doc,
                "tags": _normalize_tags(payload.tags),
                "summary": _clean_optional_text(payload.summary),
                "attachment_ids": list(derived.attachment_file_ids),
            }
        else:
            _check_cipher(payload.cipher, payload.lock_key_version)
            if payload.body or payload.doc or payload.tags or payload.summary:
                raise NotesValidationError("An encrypted note cannot carry plaintext content.")
            derived = Derived()
            write = {
                "format": "encrypted",
                "write_id": payload.write_id,
                "base_revision": payload.base_revision,
                "title": title,
                "cipher": payload.cipher,
                "lock_key_version": payload.lock_key_version,
                "attachment_ids": _check_attachment_ids(payload.attachment_ids),
                "keep_original": payload.keep_original,
            }
        try:
            return Note(**store.write_content(note_id, write, derived))
        except ValueError as exc:
            if str(exc) == "write_id_reused":
                raise NotesValidationError("That write id belongs to another note.") from exc
            if str(exc) == "lock_required":
                raise NotesValidationError("Lock the note before writing ciphertext.") from exc
            raise

    def update_note(self, note_id: str, payload: NoteUpdate) -> Note | None:
        """The original field-by-field PATCH, kept for the API's existing callers."""

        updates: dict[str, Any] = {}
        derived: Derived | None = None
        if payload.title is not None:
            title = _clean_title(payload.title)
            if not title:
                raise NotesValidationError("Title cannot be empty.")
            updates["title"] = title
        if payload.body is not None:
            body = _check_body(payload.body.strip())
            updates["body"] = body
            derived = derive_from_markdown(body)
        if payload.tags is not None:
            updates["tags"] = _normalize_tags(payload.tags)
        if payload.summary is not None:
            updates["summary"] = _clean_optional_text(payload.summary)
        for field in ("source_type", "source_id", "source_url", "source_title"):
            value = getattr(payload, field)
            if value is not None:
                updates[field] = _clean_optional_text(value)
        if payload.source_metadata is not None:
            updates["source_metadata"] = payload.source_metadata
        if not updates:
            return self.get_note(note_id)
        if payload.base_revision is not None:
            updates["base_revision"] = payload.base_revision
        try:
            return Note(**store.update_legacy(note_id, updates, derived))
        except store.NoteNotFound:
            return None

    # ------------------------------------------------------------ metadata --

    def set_pinned(self, note_id: str, pinned: bool) -> Note | None:
        return self._metadata(note_id, {"pinned": pinned})

    def set_archived(self, note_id: str, archived: bool) -> Note | None:
        return self._metadata(note_id, {"archived": archived})

    def move(self, note_id: str, folder_id: str | None) -> Note | None:
        try:
            return self._metadata(note_id, {"folder_id": folder_id or None})
        except store.FolderNotFound as exc:
            raise NotesValidationError("That folder no longer exists.") from exc

    def _metadata(self, note_id: str, fields: dict) -> Note | None:
        try:
            return Note(**store.update_metadata(note_id, fields))
        except store.NoteNotFound:
            return None

    def add_attachment(self, *, filename: str, content: bytes, mime_type: str | None) -> dict:
        """Store a file a note is about to show; the note's next save references it.

        Made with source ``note``, so once no note, version or conflict refers to
        it any more (and the grace period has passed) it is collected. Whether it
        shows as a picture or as a file is decided from its bytes, not from the
        type the browser claimed.
        """

        from app.services.files.service import WorkspaceFilesService

        image_type = sniff_image_type(content)
        # A claimed picture whose bytes are not one is stored as plain bytes.
        if not image_type and (mime_type or "").lower().startswith("image/"):
            mime_type = "application/octet-stream"
        item = WorkspaceFilesService().import_bytes(
            original_filename=filename or "attachment",
            content=content,
            mime_type=image_type or mime_type,
            source_type="note",
        )
        return {
            "id": item["id"],
            "name": item.get("display_name") or filename,
            "mime_type": image_type
            or item.get("mime_type")
            or mime_type
            or "application/octet-stream",
            "size": item.get("size_bytes") or len(content),
            "kind": "image" if image_type else "file",
            "url": f"/api/files/{item['id']}/download",
        }

    def add_encrypted_attachment(self, *, content: bytes) -> dict:
        """Ciphertext for a locked note: opaque bytes, never deduplicated, nothing extracted."""

        from app.services.files.service import WorkspaceFilesService

        item = WorkspaceFilesService().import_bytes(
            original_filename="encrypted",
            content=content,
            mime_type="application/octet-stream",
            source_type="note_encrypted",
        )
        return {
            "id": item["id"],
            "size": item.get("size_bytes") or len(content),
            "url": f"/api/files/{item['id']}/download",
        }

    def soft_delete(self, note_id: str) -> Note | None:
        try:
            return Note(**store.move_to_trash(note_id))
        except store.NoteNotFound:
            return None

    def restore(self, note_id: str, folder_id: str | None = None) -> Note | None:
        try:
            return Note(**store.restore_from_trash(note_id, folder_id))
        except store.NoteNotFound:
            return None

    def delete_permanently(self, note_id: str) -> bool:
        return store.permanently_delete(note_id)

    def empty_trash(self) -> int:
        deleted = 0
        for note_id in store.trashed_note_ids():
            deleted += bool(store.permanently_delete(note_id))
        return deleted

    def duplicate(self, note_id: str) -> Note | None:
        try:
            return Note(**store.duplicate_note(note_id))
        except store.NoteNotFound:
            return None

    # ------------------------------------------------------------- folders --

    def create_folder(self, payload: NoteFolderCreate) -> NoteFolder:
        name = _clean_folder_name(payload.name)
        try:
            return NoteFolder(**store.create_folder(name, payload.parent_id))
        except store.FolderNotFound as exc:
            raise NotesValidationError("The parent folder no longer exists.") from exc

    def update_folder(self, folder_id: str, payload: NoteFolderUpdate) -> NoteFolder | None:
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = _clean_folder_name(payload.name)
        if payload.move_to_root:
            updates["parent_id"] = None
        elif payload.parent_id is not None:
            updates["parent_id"] = payload.parent_id
        if payload.position is not None:
            updates["position"] = payload.position
        try:
            return NoteFolder(**store.update_folder(folder_id, updates))
        except store.InvalidFolderMove as exc:
            raise NotesValidationError(str(exc)) from exc
        except store.FolderNotFound as exc:
            if str(exc) == folder_id:
                return None
            raise NotesValidationError("The parent folder no longer exists.") from exc

    def delete_folder(self, folder_id: str) -> dict | None:
        try:
            return store.delete_folder(folder_id)
        except store.FolderNotFound:
            return None

    # --------------------------------------------------- versions/conflicts --

    def list_versions(self, note_id: str) -> list[NoteVersionSummary]:
        return [NoteVersionSummary(**row) for row in store.list_versions(note_id)]

    def get_version(self, note_id: str, version_id: str) -> NoteVersion | None:
        row = store.get_version(note_id, version_id)
        return NoteVersion(**row) if row else None

    def restore_version(
        self, note_id: str, version_id: str, base_revision: int, write_id: str
    ) -> Note:
        return Note(**store.restore_version(note_id, version_id, base_revision, write_id))

    def list_conflicts(self, note_id: str, *, include_resolved: bool = False) -> list[NoteConflict]:
        return [
            NoteConflict(**row)
            for row in store.list_conflicts(note_id, include_resolved=include_resolved)
        ]

    def resolve_conflict(
        self, note_id: str, conflict_id: str, payload: ConflictResolution
    ) -> Note | None:
        conflict = store.get_conflict(note_id, conflict_id)
        if conflict is None or conflict.get("resolved_at"):
            return None
        derived = _derived_for_conflict(conflict)
        if payload.resolution == "keep_mine":
            if payload.base_revision is None or not payload.write_id:
                raise NotesValidationError("Keeping your version needs the current revision.")
            return Note(
                **store.resolve_conflict_keep_mine(
                    note_id, conflict_id, payload.base_revision, payload.write_id, derived
                )
            )
        if payload.resolution == "use_other":
            return Note(**store.resolve_conflict_use_other(note_id, conflict_id))
        stamp = datetime.now(UTC).strftime("%d %b %Y %H:%M")
        title = _clean_title(f"{conflict['title'] or UNTITLED} (conflict copy, {stamp})")
        copy = {"id": str(uuid.uuid4()), "title": title or UNTITLED, "derived": derived}
        return Note(**store.resolve_conflict_save_copy(note_id, conflict_id, copy))

    # ------------------------------------------------------------ settings --

    def get_settings(self) -> NoteSettings:
        stored = store.get_setting("prefs") or {}
        try:
            return NoteSettings.model_validate(stored)
        except ValidationError:
            return NoteSettings()

    def update_settings(self, payload: NoteSettingsUpdate) -> NoteSettings:
        merged = {
            **self.get_settings().model_dump(),
            **payload.model_dump(exclude_none=True),
        }
        settings = NoteSettings.model_validate(merged)
        store.set_setting("prefs", settings.model_dump())
        return settings

    # ------------------------------------------------------------ research --

    def save_research_report(self, job: ResearchJob, *, title: str | None, tags: list[str]) -> Note:
        existing = store.find_note_by_source("research_report", job.id)
        if existing:
            return Note(**existing)
        if job.status != JobStatus.COMPLETED or not job.report.strip():
            raise NotesValidationError("Research report is not ready.")
        source_title = _research_title(job)
        all_tags = _normalize_tags(["research", *tags])
        metadata = {
            "mode": job.depth.value if hasattr(job.depth, "value") else str(job.depth),
            "confidence": job.metadata.get("confidence") if job.metadata else None,
            "report_type": job.metadata.get("report_type") if job.metadata else None,
            "generated_at": job.updated_at or job.created_at,
            "sources_count": sum(1 for source in job.sources if source.used),
            "evidence_count": sum(1 for item in job.evidence if item.status == "accepted"),
        }
        note = self.create_note(
            NoteCreate(
                title=title or source_title,
                body=job.report,
                tags=all_tags,
                source_type="research_report",
                source_id=job.id,
                source_title=source_title,
                source_metadata={key: val for key, val in metadata.items() if val is not None},
            )
        )
        store.insert_link(
            {
                "id": str(uuid.uuid4()),
                "note_id": note.id,
                "link_type": "research_job",
                "target_id": job.id,
                "title": source_title,
                "metadata": note.source_metadata,
                "created_at": store.now_iso(),
            }
        )
        return note


def _derived_for_conflict(conflict: dict) -> Derived:
    fmt = conflict["content_format"]
    if fmt == "rich" and conflict.get("doc") is not None:
        return derive_from_doc(conflict["doc"], conflict.get("body") or "")
    if fmt == "markdown":
        return derive_from_markdown(conflict.get("body") or "")
    return Derived()


def _check_doc(doc: dict[str, Any]) -> None:
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        raise NotesValidationError("The note document is not valid.")
    if len(json.dumps(doc, separators=(",", ":"))) > MAX_DOC_BYTES:
        raise NotesValidationError("This note is too large to save.")


def _check_body(body: str) -> str:
    if len(body) > MAX_BODY_LENGTH:
        raise NotesValidationError("Body is too long.")
    return body


def _check_cipher(cipher: dict[str, Any] | None, key_version: int | None) -> None:
    if not isinstance(cipher, dict):
        raise NotesValidationError("Encrypted content is missing.")
    for key in ("v", "iv", "ct"):
        if key not in cipher:
            raise NotesValidationError("Encrypted content is malformed.")
    if not isinstance(cipher.get("iv"), str) or not isinstance(cipher.get("ct"), str):
        raise NotesValidationError("Encrypted content is malformed.")
    if len(json.dumps(cipher)) > MAX_CIPHER_BYTES:
        raise NotesValidationError("This note is too large to save.")
    if not isinstance(key_version, int) or key_version < 1:
        raise NotesValidationError("Encrypted content needs its key version.")


def _check_attachment_ids(ids: list[str]) -> list[str]:
    cleaned = list(dict.fromkeys(ids))
    if any(not _UUID.match(item) for item in cleaned):
        raise NotesValidationError("Attachment ids are malformed.")
    return cleaned


def _clean_title(title: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", (title or "")).strip()
    return cleaned[:MAX_TITLE_LENGTH]


def _clean_title_body(title: str | None, body: str) -> tuple[str, str]:
    cleaned_body = _check_body(body.strip())
    cleaned_title = _clean_title(title)
    if not cleaned_title:
        if not cleaned_body:
            raise NotesValidationError("Title or body is required.")
        cleaned_title = _derive_title(cleaned_body)
    return cleaned_title, cleaned_body


def _derive_title(body: str) -> str:
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:MAX_TITLE_LENGTH]
    raise NotesValidationError("Title or body is required.")


def _clean_folder_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not cleaned:
        raise NotesValidationError("A folder needs a name.")
    return cleaned[:100]


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = _normalize_tag(raw)
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) >= MAX_TAGS:
            break
    return cleaned


def _normalize_tag(tag: str) -> str:
    cleaned = re.sub(r"\s+", "-", tag.strip().lower().lstrip("#"))
    cleaned = cleaned[:MAX_TAG_LENGTH]
    return cleaned


def _research_title(job: ResearchJob) -> str:
    return job.user_query[:MAX_TITLE_LENGTH]
