from __future__ import annotations

from pathlib import Path
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
ConnectorAuthType = Literal[
    "none",
    "api_key_header",
    "api_key_query",
    "bearer",
    "oauth2",
]


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
        if Path(clean[0]).name.lower() in {
            "bash",
            "cmd",
            "cmd.exe",
            "dash",
            "fish",
            "powershell",
            "pwsh",
            "sh",
            "zsh",
        }:
            raise ValueError("MCP stdio commands may not invoke a command shell.")
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

    @field_validator("command_json")
    @classmethod
    def command_must_be_argv(cls, value: list[str] | None) -> list[str] | None:
        return ToolServerBase.command_must_be_argv(value)


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


class ConnectorCredentialWrite(BaseModel):
    """Write-only credential payload.

    Secret fields are accepted by the API but are never represented by the
    corresponding read model.
    """

    auth_type: ConnectorAuthType
    label: str | None = Field(default=None, max_length=120)
    secret: str | None = Field(default=None, min_length=1, max_length=16000)
    header_name: str | None = Field(default=None, max_length=120)
    query_name: str | None = Field(default=None, max_length=120)
    client_id: str | None = Field(default=None, max_length=1000)
    client_secret: str | None = Field(default=None, max_length=16000)
    authorization_url: str | None = Field(default=None, max_length=2000)
    token_url: str | None = Field(default=None, max_length=2000)
    revocation_url: str | None = Field(default=None, max_length=2000)
    redirect_uri: str | None = Field(default=None, max_length=2000)
    scopes: list[str] = Field(default_factory=list, max_length=100)
    extra_token_params: dict[str, str] = Field(default_factory=dict)


class ConnectorCredentialStatus(BaseModel):
    server_id: str
    configured: bool
    auth_type: ConnectorAuthType = "none"
    label: str | None = None
    client_id: str | None = None
    header_name: str | None = None
    query_name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: str | None = None
    has_refresh_token: bool = False
    created_at: str | None = None
    updated_at: str | None = None


class OpenAPIImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    document: dict[str, Any] | str | None = None
    document_url: str | None = Field(default=None, max_length=2000)
    enabled: bool = True
    allow_trusted_localhost: bool = False
    default_write_approval: bool = True


class ManualRestToolRequest(BaseModel):
    server_id: str | None = Field(default=None, max_length=120)
    server_name: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=2000)
    name: str = Field(min_length=1, max_length=120)
    display_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=2000)
    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(min_length=1, max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    parameter_locations: dict[str, Literal["path", "query", "header", "body"]] = Field(
        default_factory=dict
    )
    read_only: bool | None = None
    allow_trusted_localhost: bool = False


class OAuthCallbackRequest(BaseModel):
    state: str = Field(min_length=16, max_length=1000)
    code: str = Field(min_length=1, max_length=16000)


class ConnectorSelectionRequest(BaseModel):
    capability: str = Field(min_length=1, max_length=200)
    intent: str | None = Field(default=None, max_length=500)
    arguments: dict[str, Any] = Field(default_factory=dict)
    invoke: bool = False
