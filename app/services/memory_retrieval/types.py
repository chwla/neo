from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

MEMORY_TYPES = {
    "decision",
    "constraint",
    "summary",
    "failure",
    "fix",
    "test_result",
    "checkpoint",
    "file_context",
    "project_note",
    "research_finding",
    "user_instruction",
    "open_item",
    "completed_item",
    "safety_note",
}
SCOPE_TYPES = {
    "chat",
    "agent_run",
    "agentic_run",
    "coding_run",
    "task",
    "project",
    "repo_workspace",
    "research_run",
    "user",
}


class MemoryItemCreate(BaseModel):
    scope_type: str
    scope_id: str
    source_type: str = "manual"
    source_id: str | None = None
    memory_type: str = "project_note"
    title: str = Field(min_length=1, max_length=240)
    content_text: str = Field(min_length=1, max_length=30_000)
    content_json: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=30)
    importance: int = Field(default=3, ge=1, le=5)
    confidence: float = Field(default=1.0, ge=0, le=1)
    expires_at: str | None = None

    @field_validator("scope_type")
    @classmethod
    def valid_scope(cls, value: str) -> str:
        if value not in SCOPE_TYPES:
            raise ValueError("Unsupported memory scope.")
        return value

    @field_validator("memory_type")
    @classmethod
    def valid_type(cls, value: str) -> str:
        if value not in MEMORY_TYPES:
            raise ValueError("Unsupported memory type.")
        return value

    @field_validator("scope_id", "title", "content_text")
    @classmethod
    def clean_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Memory text is required.")
        return value


class MemoryItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    content_text: str | None = Field(default=None, min_length=1, max_length=30_000)
    content_json: dict[str, Any] | None = None
    tags: list[str] | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: str | None = None


class MemoryRetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    scope_type: str | None = None
    scope_id: str | None = None
    memory_types: list[str] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=50)
    include_score_breakdown: bool = True
    created_by: str = "user"


class MemoryIndexRequest(BaseModel):
    scope_type: str | None = None
    scope_id: str | None = None
    source_type: str | None = None
    source_id: str | None = None
    source_types: list[str] = Field(default_factory=list)


class PruneRequest(BaseModel):
    stale_days: int = Field(default=180, ge=1, le=3650)
    apply: bool = False
    confirm: bool = False
