from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ScopeType = Literal["chat", "agent_run", "coding_run", "task", "project", "repo_workspace"]


class CompactRequest(BaseModel):
    scope_type: ScopeType
    scope_id: str = Field(min_length=1, max_length=200)
    mode: Literal["safe", "deterministic", "llm"] = "safe"
    max_summary_tokens: int = Field(default=1200, ge=100, le=5000)
    include_events: bool = True
    include_files: bool = True
    include_tests: bool = True
    include_checkpoints: bool = True


class ContextEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=80)
    event_ref_id: str | None = Field(default=None, max_length=200)
    importance: int = Field(default=3, ge=1, le=5)
    content: dict[str, Any] = Field(default_factory=dict)
