from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import app.services.notes.store as store
from app.api.routes.accounts import session_for
from app.core.config import get_settings as app_settings
from app.services.notes import (
    Note,
    NoteCreate,
    NoteListItem,
    NotesService,
    NoteTag,
    NoteUpdate,
    transitions,
)
from app.services.notes import keys as note_keys
from app.services.notes.service import NotesValidationError
from app.services.notes.types import (
    ConflictResolution,
    NoteConflict,
    NoteContentWrite,
    NoteFolder,
    NoteFolderCreate,
    NoteFolderUpdate,
    NoteSettings,
    NoteSettingsUpdate,
    NoteSidebar,
    NoteVersion,
    NoteVersionSummary,
    SmartFolder,
    SmartFolderCreate,
    SmartFolderUpdate,
    TagRename,
    VersionRestore,
)
from app.services.profile_accounts import authenticate


def _ready() -> None:
    """Refuse every notes request while this profile's notes upgrade is refused."""

    failure = store.migration_failure()
    if failure:
        raise HTTPException(
            503,
            {
                "code": "notes_upgrade_failed",
                "message": "Neo couldn't upgrade the notes database. Your notes are unchanged.",
            },
        )


router = APIRouter(prefix="/notes", tags=["notes"], dependencies=[Depends(_ready)])


class NoteResponse(BaseModel):
    note: Note
    # True when a create was a replay of one that already ran (same write id):
    # the note returned is the one that create made, with the content it had.
    replayed: bool = False


class NotesListResponse(BaseModel):
    notes: list[NoteListItem]
    total: int


class TagsResponse(BaseModel):
    tags: list[NoteTag]


class PinRequest(BaseModel):
    pinned: bool


class ArchiveRequest(BaseModel):
    archived: bool


class MoveRequest(BaseModel):
    folder_id: str | None = None


class RestoreRequest(BaseModel):
    folder_id: str | None = None


class KeyPassword(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class KeyRewrap(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    earlier_password: str = Field(min_length=1, max_length=200)


class TransitionBegin(BaseModel):
    kind: Literal["lock", "unlock", "rekey"]
    base_revision: int


class TransitionForm(BaseModel):
    """One piece of content in its new form: ciphertext (lock, re-key) or plain (Remove Lock)."""

    title: str | None = Field(default=None, max_length=1000)
    format: Literal["rich", "markdown"] | None = None
    doc: dict[str, Any] | None = None
    body: str | None = None
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    cipher: dict[str, Any] | None = None
    lock_key_version: int | None = None
    attachment_ids: list[str] = Field(default_factory=list)


class TransitionCommit(BaseModel):
    content: TransitionForm
    versions: dict[str, TransitionForm] = Field(default_factory=dict)
    conflicts: dict[str, TransitionForm] = Field(default_factory=dict)


def _service() -> NotesService:
    return NotesService()


def _current(note_id: str) -> dict | None:
    note = store.get_note(note_id, include_deleted=True)
    return Note(**note).model_dump() if note else None


@contextmanager
def _errors() -> Iterator[None]:
    """One translation from store outcomes to HTTP, so every route answers alike."""

    try:
        yield
    except NotesValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except store.ConflictCaptured as exc:
        raise HTTPException(
            409,
            {
                "code": "conflict",
                "reason": exc.reason,
                "conflict_id": exc.conflict_id,
                "note": _current(exc.note_id),
            },
        ) from exc
    except store.StaleRevision as exc:
        raise HTTPException(409, {"code": "stale", "note": _current(str(exc))}) from exc
    except store.NoteLocked as exc:
        raise HTTPException(
            409,
            {"code": "locked", "message": "This note is locked.", "note": _current(str(exc))},
        ) from exc
    except store.NoteDeleted as exc:
        raise HTTPException(
            409,
            {
                "code": "note_deleted",
                "message": "This note is in Recently Deleted. Restore it first.",
                "note": _current(str(exc)),
            },
        ) from exc
    except store.NotInTrash as exc:
        raise HTTPException(
            409,
            {"code": "not_in_trash", "message": "Only notes in Recently Deleted can be erased."},
        ) from exc
    except store.NoteGone as exc:
        raise HTTPException(
            410, {"code": "gone", "message": "This note was permanently deleted."}
        ) from exc
    except store.NoteNotFound as exc:
        raise HTTPException(404, "Note not found.") from exc
    except store.FolderNotFound as exc:
        raise HTTPException(404, "Folder not found.") from exc
    except store.SmartFolderNotFound as exc:
        raise HTTPException(404, "Smart folder not found.") from exc
    except transitions.TransitionNotFound as exc:
        raise HTTPException(404, "That change isn't known.") from exc
    except transitions.TransitionClosed as exc:
        raise HTTPException(
            409,
            {"code": "transition_closed", "message": "That change was abandoned. Start it again."},
        ) from exc
    except transitions.TransitionInvalid as exc:
        raise HTTPException(400, {"code": "transition_invalid", "message": str(exc)}) from exc


# ------------------------------------------------------------ collections --


@router.post("", response_model=NoteResponse)
def create_note(payload: NoteCreate):
    with _errors():
        note, replayed = _service().create(payload)
        return NoteResponse(note=note, replayed=replayed)


@router.post("/attachments")
async def upload_attachment(file: Annotated[UploadFile, File()], encrypted: bool = False) -> dict:
    """A picture or file for a note. The note's next save is what references it.

    ``encrypted``: for a locked note, already encrypted in the browser. It is
    kept as opaque bytes, with no name, type or text of its own.
    """

    limit = app_settings().workspace_file_max_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"Attachments can be up to {limit // (1024 * 1024)} MB.")
    try:
        if encrypted:
            return {"attachment": _service().add_encrypted_attachment(content=content)}
        attachment = _service().add_attachment(
            filename=file.filename or "attachment", content=content, mime_type=file.content_type
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"attachment": attachment}


@router.get("", response_model=NotesListResponse)
def list_notes(
    q: str | None = None,
    tag: str | None = None,
    tags: str | None = None,
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
):
    if view not in (None, "all", "folder", "archived", "deleted", "tag", "smart"):
        raise HTTPException(400, "Unknown view.")
    if sort not in ("edited", "created", "title") or order not in ("asc", "desc"):
        raise HTTPException(400, "Unknown sort.")
    with _errors():
        notes, total = _service().list_notes(
            q=q,
            tag=tag,
            tags=[item for item in (tags or "").split(",") if item.strip()],
            tags_mode=tags_mode,
            include_archived=include_archived,
            pinned_first=pinned_first,
            view=None if view == "all" else view,
            folder_id=folder_id,
            smart_id=smart_id,
            tz_offset=tz_offset,
            sort=sort,
            order=order,
            include_body=include_body,
            limit=limit,
            offset=offset,
        )
    return NotesListResponse(notes=notes, total=total)


@router.get("/tags", response_model=TagsResponse)
def get_tags():
    return TagsResponse(tags=_service().list_tags())


class RetagResponse(BaseModel):
    #: The notes rewritten, with their new revisions.
    notes: list[dict]


@router.post("/tags/{tag}/rename", response_model=RetagResponse)
def rename_tag(tag: str, payload: TagRename):
    with _errors():
        return RetagResponse(notes=_service().rename_tag(tag, payload.name))


@router.delete("/tags/{tag}", response_model=RetagResponse)
def delete_tag(tag: str):
    with _errors():
        return RetagResponse(notes=_service().delete_tag(tag))


@router.get("/smart-folders", response_model=list[SmartFolder])
def list_smart_folders(tz_offset: int = 0):
    return [SmartFolder(**folder) for folder in store.list_smart_folders(tz_offset=tz_offset)]


@router.post("/smart-folders", response_model=SmartFolder)
def create_smart_folder(payload: SmartFolderCreate):
    with _errors():
        return _service().create_smart_folder(payload)


@router.patch("/smart-folders/{smart_id}", response_model=SmartFolder)
def update_smart_folder(smart_id: str, payload: SmartFolderUpdate):
    with _errors():
        return _service().update_smart_folder(smart_id, payload)


@router.delete("/smart-folders/{smart_id}", status_code=204)
def delete_smart_folder(smart_id: str):
    if not _service().delete_smart_folder(smart_id):
        raise HTTPException(404, "Smart folder not found.")


@router.get("/sidebar", response_model=NoteSidebar)
def get_sidebar(tz_offset: int = 0):
    return _service().sidebar(tz_offset=tz_offset)


# ------------------------------------------------------------------ keys --
#
# Locked notes open with the profile's own password (see services/notes/keys.py).
# Each call checks it exactly as sign-in does, with the same answer when it is
# wrong; the key itself is unwrapped in the browser, not here.


def _password_profile(request: Request) -> dict:
    profile = session_for(request)
    if profile is None:
        raise HTTPException(401, "authenticated_profile_required")
    if profile.get("is_guest"):
        raise HTTPException(
            403,
            {
                "code": "guest",
                "message": "Guest sessions have no password, so they can't lock notes.",
            },
        )
    return profile


@router.get("/keys")
def key_state(request: Request) -> dict:
    profile = session_for(request)
    return {**note_keys.state(), "guest": bool(profile and profile.get("is_guest"))}


@router.post("/keys/setup")
def setup_keys(payload: KeyPassword, request: Request) -> dict:
    authenticate(_password_profile(request)["id"], payload.password)
    return note_keys.setup(payload.password)


@router.post("/keys/unlock")
def unlock_keys(payload: KeyPassword, request: Request) -> dict:
    authenticate(_password_profile(request)["id"], payload.password)
    try:
        return note_keys.unlock(payload.password)
    except note_keys.KeysNotSetUp as exc:
        raise HTTPException(
            404, {"code": "keys_not_set_up", "message": "No note has been locked yet."}
        ) from exc


@router.post("/keys/rewrap")
def rewrap_keys(payload: KeyRewrap, request: Request) -> dict:
    authenticate(_password_profile(request)["id"], payload.password)
    try:
        return note_keys.rewrap(payload.password, payload.earlier_password)
    except note_keys.EarlierPasswordWrong as exc:
        raise HTTPException(
            401,
            {
                "code": "earlier_password_wrong",
                "message": "That password doesn't open these notes.",
            },
        ) from exc


@router.get("/settings", response_model=NoteSettings)
def get_settings():
    return _service().get_settings()


@router.patch("/settings", response_model=NoteSettings)
def update_settings(payload: NoteSettingsUpdate):
    return _service().update_settings(payload)


@router.post("/folders", response_model=NoteFolder)
def create_folder(payload: NoteFolderCreate):
    with _errors():
        return _service().create_folder(payload)


@router.patch("/folders/{folder_id}", response_model=NoteFolder)
def update_folder(folder_id: str, payload: NoteFolderUpdate):
    with _errors():
        folder = _service().update_folder(folder_id, payload)
    if folder is None:
        raise HTTPException(404, "Folder not found.")
    return folder


@router.delete("/folders/{folder_id}")
def delete_folder(folder_id: str):
    result = _service().delete_folder(folder_id)
    if result is None:
        raise HTTPException(404, "Folder not found.")
    return result


@router.delete("/trash")
def empty_trash():
    return {"deleted": _service().empty_trash()}


# ------------------------------------------------------------------ notes --


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(note_id: str, include_deleted: bool = False):
    note = _service().get_note(note_id, include_deleted=include_deleted)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.put("/{note_id}/content", response_model=NoteResponse)
def write_content(note_id: str, payload: NoteContentWrite):
    with _errors():
        return NoteResponse(note=_service().write_content(note_id, payload))


@router.patch("/{note_id}", response_model=NoteResponse)
def update_note(note_id: str, payload: NoteUpdate):
    with _errors():
        note = _service().update_note(note_id, payload)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.post("/{note_id}/pin", response_model=NoteResponse)
def pin_note(note_id: str, payload: PinRequest):
    note = _service().set_pinned(note_id, payload.pinned)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.post("/{note_id}/archive", response_model=NoteResponse)
def archive_note(note_id: str, payload: ArchiveRequest):
    note = _service().set_archived(note_id, payload.archived)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.post("/{note_id}/move", response_model=NoteResponse)
def move_note(note_id: str, payload: MoveRequest):
    with _errors():
        note = _service().move(note_id, payload.folder_id)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.post("/{note_id}/restore", response_model=NoteResponse)
def restore_note(note_id: str, payload: RestoreRequest | None = None):
    note = _service().restore(note_id, payload.folder_id if payload else None)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.post("/{note_id}/duplicate", response_model=NoteResponse)
def duplicate_note(note_id: str):
    with _errors():
        note = _service().duplicate(note_id)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.delete("/{note_id}")
def delete_note(note_id: str, permanent: bool = False):
    if permanent:
        with _errors():
            if not _service().delete_permanently(note_id):
                raise HTTPException(404, "Note not found.")
        return {"deleted": True, "permanent": True}
    note = _service().soft_delete(note_id)
    if note is None:
        raise HTTPException(404, "Note not found.")
    # The new meta revision lets a client tell this delete from a list it
    # fetched before it.
    return {"deleted": True, "deleted_at": note.deleted_at, "meta_revision": note.meta_revision}


# ----------------------------------------------------------- transitions --
#
# Lock, Remove Lock and re-key (services/notes/transitions.py): begin, stage
# each prepared attachment, then commit everything at once, or abort.


@router.post("/{note_id}/transitions")
def begin_transition(note_id: str, payload: TransitionBegin) -> dict:
    with _errors():
        return transitions.begin(note_id, payload.kind, payload.base_revision)


@router.post("/{note_id}/transitions/{transition_id}/files")
async def stage_transition_file(
    note_id: str, transition_id: str, file: Annotated[UploadFile, File()]
) -> dict:
    limit = app_settings().workspace_file_max_bytes
    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"Attachments can be up to {limit // (1024 * 1024)} MB.")
    with _errors():
        try:
            return transitions.stage_file(
                note_id,
                transition_id,
                content=content,
                name=file.filename or "attachment",
                mime_type=file.content_type,
            )
        except ValueError as exc:
            if isinstance(exc, transitions.TransitionInvalid):
                raise
            raise HTTPException(400, str(exc)) from exc


@router.post("/{note_id}/transitions/{transition_id}/commit")
def commit_transition(note_id: str, transition_id: str, payload: TransitionCommit) -> dict:
    with _errors():
        result = transitions.commit(
            note_id,
            transition_id,
            {
                "content": payload.content.model_dump(),
                "versions": {k: v.model_dump() for k, v in payload.versions.items()},
                "conflicts": {k: v.model_dump() for k, v in payload.conflicts.items()},
            },
        )
    return {**result, "note": _current(note_id)}


@router.post("/{note_id}/transitions/{transition_id}/abort")
def abort_transition(note_id: str, transition_id: str) -> dict:
    with _errors():
        return transitions.abort(note_id, transition_id)


@router.get("/{note_id}/versions", response_model=list[NoteVersionSummary])
def list_versions(note_id: str):
    return _service().list_versions(note_id)


@router.get("/{note_id}/versions/{version_id}", response_model=NoteVersion)
def get_version(note_id: str, version_id: str):
    version = _service().get_version(note_id, version_id)
    if version is None:
        raise HTTPException(404, "Version not found.")
    return version


@router.post("/{note_id}/versions/{version_id}/restore", response_model=NoteResponse)
def restore_version(note_id: str, version_id: str, payload: VersionRestore):
    with _errors():
        return NoteResponse(
            note=_service().restore_version(
                note_id, version_id, payload.base_revision, payload.write_id
            )
        )


@router.get("/{note_id}/conflicts", response_model=list[NoteConflict])
def list_conflicts(note_id: str, include_resolved: bool = False):
    # Resolved ones too when a note changes form: they still hold its content.
    return _service().list_conflicts(note_id, include_resolved=include_resolved)


@router.post("/{note_id}/conflicts/{conflict_id}/resolve", response_model=NoteResponse)
def resolve_conflict(note_id: str, conflict_id: str, payload: ConflictResolution):
    with _errors():
        note = _service().resolve_conflict(note_id, conflict_id, payload)
    if note is None:
        raise HTTPException(404, "Conflict not found.")
    return NoteResponse(note=note)
