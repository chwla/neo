from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ToolCategory = Literal[
    "read_only",
    "workspace_read",
    "workspace_write_approval_required",
    "external_read",
    "external_write_approval_required",
    "dangerous_disabled",
]
ToolCallStatus = Literal["pending_approval", "completed", "failed", "rejected", "blocked"]
ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]
ServerType = Literal["builtin", "stdio", "http"]
SkillType = Literal["instruction_bundle", "workflow", "checklist"]


class ToolServerBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    server_type: ServerType
    command_json: list[str] | None = None
    url: str | None = Field(default=None, max_length=2000)
    env_json: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    approval_required: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("command_json")
    @classmethod
    def command_must_be_argv(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        clean = [str(item).strip() for item in value if str(item).strip()]
        if not clean:
            raise ValueError("command_json must be a non-empty argv array.")
        forbidden = {";", "&&", "||", "|", "`", "$(", ">", "<"}
        joined = " ".join(clean)
        if any(token in joined for token in forbidden):
            raise ValueError("MCP stdio commands must be argv arrays, not shell strings.")
        return clean


class ToolServerCreate(ToolServerBase):
    id: str | None = Field(default=None, max_length=120)


class ToolServerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    command_json: list[str] | None = None
    url: str | None = Field(default=None, max_length=2000)
    env_json: dict[str, str] | None = None
    enabled: bool | None = None
    approval_required: bool | None = None
    metadata: dict[str, Any] | None = None


class ToolServer(ToolServerBase):
    id: str
    created_at: str
    updated_at: str


class ToolDefinitionBase(BaseModel):
    server_id: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: ToolCategory
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    built_in: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolDefinitionCreate(ToolDefinitionBase):
    id: str | None = Field(default=None, max_length=120)


class ToolDefinitionUpdate(BaseModel):
    server_id: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    category: ToolCategory | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class ToolDefinition(ToolDefinitionBase):
    id: str
    created_at: str
    updated_at: str


class SkillDefinitionBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    skill_type: SkillType = "instruction_bundle"
    instructions: str = Field(min_length=1, max_length=12000)
    tool_ids: list[str] = Field(default_factory=list)
    agent_ids: list[str] = Field(default_factory=list)
    rules_profile_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    built_in: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillDefinitionCreate(SkillDefinitionBase):
    id: str | None = Field(default=None, max_length=120)


class SkillDefinitionUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    skill_type: SkillType | None = None
    instructions: str | None = Field(default=None, min_length=1, max_length=12000)
    tool_ids: list[str] | None = None
    agent_ids: list[str] | None = None
    rules_profile_ids: list[str] | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class SkillDefinition(SkillDefinitionBase):
    id: str
    created_at: str
    updated_at: str


class ToolCallCreate(BaseModel):
    tool_id: str = Field(min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    run_id: str | None = Field(default=None, max_length=120)
    coding_run_id: str | None = Field(default=None, max_length=120)
    agent_definition_id: str | None = Field(default=None, max_length=120)
    skill_id: str | None = Field(default=None, max_length=120)


class ToolCall(BaseModel):
    id: str
    run_id: str | None = None
    coding_run_id: str | None = None
    agent_definition_id: str | None = None
    tool_id: str
    skill_id: str | None = None
    status: ToolCallStatus
    approval_status: ApprovalStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: int | None = None
    created_at: str
    completed_at: str | None = None
