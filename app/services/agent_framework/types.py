from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AgentType = Literal[
    "general",
    "planner",
    "coder",
    "reviewer",
    "tester",
    "researcher",
    "refactor",
    "explorer",
    "summarizer",
    "custom",
]
DelegationStatus = Literal["pending", "running", "completed", "failed", "cancelled", "rejected"]


class AgentPermissions(BaseModel):
    can_plan: bool = True
    can_read_files: bool = True
    can_select_context: bool = True
    can_propose_patch: bool = False
    can_request_patch_apply: bool = False
    can_request_tests: bool = False
    can_request_checkpoint: bool = False
    can_research: bool = False
    can_delegate: bool = False
    max_delegations: int = Field(default=0, ge=0, le=5)
    allowed_file_patterns: list[str] = Field(default_factory=list)
    forbidden_file_patterns: list[str] = Field(default_factory=list)


class AgentDefinitionBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    agent_type: AgentType = "custom"
    system_prompt: str = Field(min_length=1, max_length=12000)
    default_route_name: str | None = Field(default=None, max_length=120)
    rules_profile_ids: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    tools: list[str] = Field(default_factory=list)
    enabled: bool = True
    priority: int = Field(default=100, ge=-10000, le=10000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentDefinitionCreate(AgentDefinitionBase):
    id: str | None = Field(default=None, max_length=120)


class AgentDefinitionUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=12000)
    default_route_name: str | None = Field(default=None, max_length=120)
    rules_profile_ids: list[str] | None = None
    permissions: AgentPermissions | None = None
    tools: list[str] | None = None
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-10000, le=10000)
    metadata: dict[str, Any] | None = None


class AgentDefinition(AgentDefinitionBase):
    id: str
    built_in: bool = False
    safety_warnings: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class DelegationCreate(BaseModel):
    parent_run_id: str = Field(min_length=1, max_length=120)
    child_agent_id: str = Field(min_length=1, max_length=120)
    delegation_type: str = Field(default="subagent", min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=10000)
    input: dict[str, Any] = Field(default_factory=dict)
    parent_agent_id: str | None = Field(default=None, max_length=120)
    child_run_id: str | None = Field(default=None, max_length=120)


class DelegationUpdate(BaseModel):
    status: DelegationStatus | None = None
    child_run_id: str | None = Field(default=None, max_length=120)
    output: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=4000)


class AgentDelegation(BaseModel):
    id: str
    parent_run_id: str
    child_run_id: str | None = None
    parent_agent_id: str | None = None
    child_agent_id: str
    delegation_type: str
    objective: str
    status: DelegationStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
