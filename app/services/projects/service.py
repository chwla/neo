from __future__ import annotations

import re
import uuid

import app.services.projects.store as store
from app.services.projects.types import (
    Project,
    ProjectCreate,
    ProjectLink,
    ProjectListItem,
    ProjectNote,
    ProjectTag,
    ProjectUpdate,
)

MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 50_000
MAX_TAG_LENGTH = 40
ALLOWED_STATUSES = {"active", "paused", "completed", "archived"}
ALLOWED_PRIORITIES = {"low", "medium", "high", "critical"}


class ProjectsValidationError(ValueError):
    pass


class ProjectsService:
    def create_project(self, payload: ProjectCreate) -> Project:
        title = _clean_title(payload.title)
        description = _clean_description(payload.description)
        status = _validate_status(payload.status)
        priority = _validate_priority(payload.priority)
        now = store.now_iso()
        project = {
            "id": str(uuid.uuid4()),
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "tags": _normalize_tags(payload.tags),
            "pinned": False,
            "archived": status == "archived",
            "deleted": False,
            "created_at": now,
            "updated_at": now,
        }
        return Project(**store.insert_project(project))

    def get_project(self, project_id: str) -> Project | None:
        project = store.get_project(project_id)
        return Project(**project) if project else None

    def read_project(self, project_id: str) -> tuple[Project, list[ProjectNote], list[ProjectLink]] | None:
        project = self.get_project(project_id)
        if project is None:
            return None
        notes = self.list_project_notes(project_id)
        links = store.list_links(project_id)
        return (
            project,
            notes,
            [ProjectLink(**link) for link in (links or [])],
        )

    def list_projects(
        self,
        *,
        q: str | None = None,
        tag: str | None = None,
        status: str | None = None,
        include_archived: bool = False,
        pinned_first: bool = True,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ProjectListItem], int]:
        cleaned_q = q.strip() if q and q.strip() else None
        cleaned_tag = _normalize_tag(tag) if tag and tag.strip() else None
        cleaned_status = _validate_status(status) if status else None
        projects, total = store.list_projects(
            q=cleaned_q,
            tag=cleaned_tag,
            status=cleaned_status,
            include_archived=include_archived,
            pinned_first=pinned_first,
            limit=max(1, min(limit, 100)),
            offset=max(0, offset),
        )
        return [
            ProjectListItem(**{**project, "preview": _preview(project["description"])})
            for project in projects
        ], total

    def update_project(self, project_id: str, payload: ProjectUpdate) -> Project | None:
        updates: dict = {}
        if payload.title is not None:
            updates["title"] = _clean_title(payload.title)
        if payload.description is not None:
            updates["description"] = _clean_description(payload.description)
        if payload.status is not None:
            updates["status"] = _validate_status(payload.status)
            updates["archived"] = payload.status == "archived"
        if payload.priority is not None:
            updates["priority"] = _validate_priority(payload.priority)
        if payload.tags is not None:
            updates["tags"] = _normalize_tags(payload.tags)
        if not updates:
            return self.get_project(project_id)
        project = store.update_project(project_id, updates)
        return Project(**project) if project else None

    def set_pinned(self, project_id: str, pinned: bool) -> Project | None:
        project = store.update_project(project_id, {"pinned": pinned})
        return Project(**project) if project else None

    def set_archived(self, project_id: str, archived: bool) -> Project | None:
        updates = {"archived": archived}
        if not archived:
            updates["status"] = "active"
        project = store.update_project(project_id, updates)
        return Project(**project) if project else None

    def soft_delete(self, project_id: str) -> bool:
        return store.update_project(project_id, {"deleted": True}) is not None

    def list_tags(self) -> list[ProjectTag]:
        return [ProjectTag(**row) for row in store.list_tags()]

    def attach_note(self, project_id: str, note_id: str) -> bool:
        return store.attach_note(project_id, note_id)

    def detach_note(self, project_id: str, note_id: str) -> bool:
        return store.detach_note(project_id, note_id)

    def list_project_notes(self, project_id: str) -> list[ProjectNote]:
        notes = store.list_project_notes(project_id)
        if notes is None:
            raise ProjectsValidationError("Project not found.")
        return [ProjectNote(**{**note, "preview": _preview(note["body"])}) for note in notes]

    def list_note_projects(self, note_id: str) -> list[ProjectListItem]:
        projects = store.list_note_projects(note_id)
        return [
            ProjectListItem(**{**project, "preview": _preview(project["description"])})
            for project in projects
        ]


class ProjectContextService:
    def context_for_prompt(self, prompt: str) -> str:
        lowered = prompt.lower()
        if not _looks_project_related(lowered):
            return "No project context loaded."
        matches = store.context_candidates(lowered, limit=2)
        if not matches:
            return "No project context loaded."
        blocks: list[str] = []
        service = ProjectsService()
        for project in matches:
            notes = service.list_project_notes(project["id"])[:4]
            lines = [
                f"Project: {project['title']}",
                f"Status: {project['status']}",
                f"Priority: {project['priority']}",
            ]
            if project.get("description"):
                lines.append(f"Description: {project['description'][:800]}")
            if project.get("tags"):
                lines.append(f"Tags: {', '.join(project['tags'][:8])}")
            if notes:
                lines.append("Linked notes:")
                for note in notes:
                    summary = note.summary or note.preview
                    lines.append(f"- {note.title}: {summary[:260]}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


def _clean_title(value: str | None) -> str:
    title = " ".join((value or "").split())
    if not title:
        raise ProjectsValidationError("Project title is required.")
    return title[:MAX_TITLE_LENGTH]


def _clean_description(value: str | None) -> str:
    description = (value or "").strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ProjectsValidationError("Project description is too long.")
    return description


def _validate_status(value: str) -> str:
    if value not in ALLOWED_STATUSES:
        raise ProjectsValidationError("Invalid project status.")
    return value


def _validate_priority(value: str) -> str:
    if value not in ALLOWED_PRIORITIES:
        raise ProjectsValidationError("Invalid project priority.")
    return value


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
    return re.sub(r"\s+", "-", tag.strip().lower())[:MAX_TAG_LENGTH]


def _preview(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:180]


def _looks_project_related(prompt_lower: str) -> bool:
    return bool(
        re.search(
            r"\b(project|status of|saved for|notes for|research for|what did i save for)\b",
            prompt_lower,
        )
    )
