from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CodingRunStatus = Literal[
    "queued",
    "planning",
    "selecting_context",
    "proposing_patch",
    "waiting_patch_approval",
    "applying_patch",
    "waiting_test_approval",
    "running_tests",
    "analyzing_test_result",
    "proposing_followup_patch",
    "waiting_checkpoint_approval",
    "creating_checkpoint",
    "completed",
    "failed",
    "cancelled",
]
ActionType = Literal[
    "apply_patch",
    "run_tests",
    "create_checkpoint",
    "restore_checkpoint",
    "revise_patch",
    "skip_tests",
    "skip_checkpoint",
    "cancel",
]
ActionStatus = Literal[
    "pending", "approved", "rejected", "executing", "completed", "failed", "cancelled"
]


class CodingRunCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=10_000)
    task_id: str | None = None
    project_id: str | None = None
    repo_id: str | None = None
    max_iterations: int = Field(default=3, ge=1, le=10)


class ActionDecisionRequest(BaseModel):
    confirm: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class ActionRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class PatchRevisionRequest(BaseModel):
    instructions: str = Field(min_length=1, max_length=4000)


class CodingAgentRun(BaseModel):
    id: str
    agent_run_id: str
    task_id: str | None = None
    project_id: str | None = None
    repo_id: str | None = None
    objective: str
    status: CodingRunStatus
    current_iteration: int = 1
    max_iterations: int = 3
    selected_files: list[dict[str, Any]] = Field(default_factory=list)
    patch_artifact_id: str | None = None
    patch_application_id: str | None = None
    test_run_id: str | None = None
    checkpoint_id: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    completed_at: str | None = None
    cancelled_at: str | None = None


class AgentActionRequest(BaseModel):
    id: str
    coding_run_id: str
    agent_run_id: str
    action_type: ActionType
    status: ActionStatus
    title: str
    description: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    decided_at: str | None = None
    executed_at: str | None = None
