from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.notes import Note, NoteCreate, NoteListItem, NoteTag, NoteUpdate, NotesService
from app.services.notes.service import NotesValidationError

router = APIRouter(prefix="/notes", tags=["notes"])


class NoteResponse(BaseModel):
    note: Note


class NotesListResponse(BaseModel):
    notes: list[NoteListItem]
    total: int


class TagsResponse(BaseModel):
    tags: list[NoteTag]


class PinRequest(BaseModel):
    pinned: bool


class ArchiveRequest(BaseModel):
    archived: bool


def _service() -> NotesService:
    return NotesService()


@router.post("", response_model=NoteResponse)
def create_note(payload: NoteCreate):
    try:
        return NoteResponse(note=_service().create_note(payload))
    except NotesValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=NotesListResponse)
def list_notes(
    q: str | None = None,
    tag: str | None = None,
    include_archived: bool = False,
    pinned_first: bool = True,
    limit: int = 50,
    offset: int = 0,
):
    notes, total = _service().list_notes(
        q=q,
        tag=tag,
        include_archived=include_archived,
        pinned_first=pinned_first,
        limit=limit,
        offset=offset,
    )
    return NotesListResponse(notes=notes, total=total)


@router.get("/tags", response_model=TagsResponse)
def get_tags():
    return TagsResponse(tags=_service().list_tags())


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(note_id: str):
    note = _service().get_note(note_id)
    if note is None:
        raise HTTPException(404, "Note not found.")
    return NoteResponse(note=note)


@router.patch("/{note_id}", response_model=NoteResponse)
def update_note(note_id: str, payload: NoteUpdate):
    try:
        note = _service().update_note(note_id, payload)
    except NotesValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
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


@router.delete("/{note_id}")
def delete_note(note_id: str):
    if not _service().soft_delete(note_id):
        raise HTTPException(404, "Note not found.")
    return {"deleted": True}
