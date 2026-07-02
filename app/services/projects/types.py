from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.notes.types import NoteListItem

ProjectStatus = Literal["active", "paused", "completed", "archived"]
ProjectPriority = Literal["low", "medium", "high", "critical"]


class Project(BaseModel):
    id: str
    title: str
    description: str = ""
    status: ProjectStatus = "active"
    priority: ProjectPriority = "medium"
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    archived: bool = False
    deleted: bool = False
    created_at: str
    updated_at: str


class ProjectListItem(Project):
    preview: str = ""
    linked_notes_count: int = 0


class ProjectCreate(BaseModel):
    title: str | None = None
    description: str = ""
    status: ProjectStatus = "active"
    priority: ProjectPriority = "medium"
    tags: list[str] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    status: ProjectStatus | None = None
    priority: ProjectPriority | None = None
    tags: list[str] | None = None


class ProjectTag(BaseModel):
    tag: str
    count: int


class ProjectLink(BaseModel):
    id: str
    project_id: str
    link_type: str
    target_id: str | None = None
    target_url: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class ProjectNote(NoteListItem):
    attached_at: str = ""
