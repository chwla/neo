from __future__ import annotations

import re
import uuid
from typing import Any

import app.services.notes.store as store
from app.services.notes.types import Note, NoteCreate, NoteListItem, NoteTag, NoteUpdate
from app.services.research.types import JobStatus, ResearchJob

MAX_TITLE_LENGTH = 200
MAX_TAG_LENGTH = 40
MAX_BODY_LENGTH = 200_000


class NotesValidationError(ValueError):
    pass


class NotesService:
    def create_note(self, payload: NoteCreate) -> Note:
        title, body = _clean_title_body(payload.title, payload.body)
        note_id = str(uuid.uuid4())
        now = store.now_iso()
        note = {
            "id": note_id,
            "title": title,
            "body": body,
            "summary": _clean_optional_text(payload.summary),
            "tags": _normalize_tags(payload.tags),
            "source_type": payload.source_type or "manual",
            "source_id": _clean_optional_text(payload.source_id),
            "source_url": _clean_optional_text(payload.source_url),
            "source_title": _clean_optional_text(payload.source_title),
            "source_metadata": payload.source_metadata or {},
            "pinned": False,
            "archived": False,
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        return Note(**store.insert_note(note))

    def get_note(self, note_id: str) -> Note | None:
        note = store.get_note(note_id)
        return Note(**note) if note else None

    def find_by_source(self, source_type: str, source_id: str) -> Note | None:
        note = store.find_note_by_source(source_type, source_id)
        return Note(**note) if note else None

    def list_notes(
        self,
        *,
        q: str | None = None,
        tag: str | None = None,
        include_archived: bool = False,
        pinned_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[NoteListItem], int]:
        cleaned_q = q.strip() if q and q.strip() else None
        cleaned_tag = _normalize_tag(tag) if tag and tag.strip() else None
        notes, total = store.list_notes(
            q=cleaned_q,
            tag=cleaned_tag,
            include_archived=include_archived,
            pinned_first=pinned_first,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        return [NoteListItem(**{**note, "preview": _preview(note["body"])}) for note in notes], total

    def update_note(self, note_id: str, payload: NoteUpdate) -> Note | None:
        updates: dict[str, Any] = {}
        if payload.title is not None:
            title = payload.title.strip()
            if not title:
                raise NotesValidationError("Title cannot be empty.")
            updates["title"] = title[:MAX_TITLE_LENGTH]
        if payload.body is not None:
            body = payload.body.strip()
            if not body:
                raise NotesValidationError("Body cannot be empty.")
            if len(body) > MAX_BODY_LENGTH:
                raise NotesValidationError("Body is too long.")
            updates["body"] = body
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
        note = store.update_note(note_id, updates)
        return Note(**note) if note else None

    def set_pinned(self, note_id: str, pinned: bool) -> Note | None:
        note = store.update_note(note_id, {"pinned": pinned})
        return Note(**note) if note else None

    def set_archived(self, note_id: str, archived: bool) -> Note | None:
        note = store.update_note(note_id, {"archived": archived})
        return Note(**note) if note else None

    def soft_delete(self, note_id: str) -> bool:
        return store.update_note(note_id, {"deleted": True}) is not None

    def list_tags(self) -> list[NoteTag]:
        return [NoteTag(**row) for row in store.list_tags()]

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
            "sources_count": sum(1 for source in job.sources if source.fetched),
            "evidence_count": len(job.evidence_chunks),
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


def _clean_title_body(title: str | None, body: str) -> tuple[str, str]:
    cleaned_body = body.strip()
    if not cleaned_body:
        raise NotesValidationError("Body cannot be empty.")
    if len(cleaned_body) > MAX_BODY_LENGTH:
        raise NotesValidationError("Body is too long.")
    cleaned_title = (title or "").strip()
    if not cleaned_title:
        cleaned_title = _derive_title(cleaned_body)
    return cleaned_title[:MAX_TITLE_LENGTH], cleaned_body


def _derive_title(body: str) -> str:
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:MAX_TITLE_LENGTH]
    raise NotesValidationError("Title or body is required.")


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
    return cleaned


def _normalize_tag(tag: str) -> str:
    cleaned = re.sub(r"\s+", "-", tag.strip().lower())
    cleaned = cleaned[:MAX_TAG_LENGTH]
    return cleaned


def _preview(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()[:180]


def _research_title(job: ResearchJob) -> str:
    if job.plan and job.plan.normalized_query:
        return job.plan.normalized_query[:MAX_TITLE_LENGTH]
    if job.plan and job.plan.objective:
        return job.plan.objective[:MAX_TITLE_LENGTH]
    return job.user_query[:MAX_TITLE_LENGTH]
