from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ScopeType = Literal["workspace", "global", "project", "repo", "task", "coding_run"]
ContextType = Literal["chat", "research", "agent", "coding_agent", "patch", "test", "git"]


class RuleProfileCreate(BaseModel):
    scope_type: ScopeType
    scope_id: str | None = None
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool = True
    priority: int = Field(default=100, ge=-10000, le=10000)
    rules: dict[str, Any] = Field(default_factory=dict)


class RuleProfileUpdate(BaseModel):
    scope_type: ScopeType | None = None
    scope_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-10000, le=10000)
    rules: dict[str, Any] | None = None


class RuleResolveRequest(BaseModel):
    context_type: ContextType
    context_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    repo_id: str | None = None
    coding_run_id: str | None = None
    override_rules: dict[str, Any] | None = None
