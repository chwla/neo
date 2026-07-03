from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SourceType = Literal[
    "manual",
    "research_report",
    "chat",
    "web_source",
    "memory_candidate",
    "project",
    "agent_run",
]


class Note(BaseModel):
    id: str
    title: str
    body: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_type: SourceType | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    pinned: bool = False
    archived: bool = False
    deleted: bool = False
    created_at: str
    updated_at: str


class NoteListItem(Note):
    preview: str = ""


class NoteSearchResult(NoteListItem):
    pass


class NoteCreate(BaseModel):
    title: str | None = None
    body: str
    tags: list[str] = Field(default_factory=list)
    summary: str | None = None
    source_type: SourceType | None = "manual"
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class NoteUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    tags: list[str] | None = None
    summary: str | None = None
    source_type: SourceType | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_title: str | None = None
    source_metadata: dict[str, Any] | None = None


class NoteTag(BaseModel):
    tag: str
    count: int


class NoteLink(BaseModel):
    id: str
    note_id: str
    link_type: str
    target_id: str | None = None
    target_url: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
