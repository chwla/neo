from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunType = Literal["agent", "coding_agent"]
RecoveryEventType = Literal[
    "detected_stuck",
    "resumed",
    "retry_requested",
    "retry_completed",
    "fork_created",
    "state_repaired",
    "cancelled",
    "failed_recovery",
]


class ConfirmRequest(BaseModel):
    confirm: bool = False


class ForkRunRequest(BaseModel):
    confirm: bool = False
    from_step_id: str | None = None
    from_action_request_id: str | None = None
    objective_override: str | None = Field(default=None, max_length=10_000)


class RetryRunRequest(BaseModel):
    confirm: bool = False
    instructions: str | None = Field(default=None, max_length=4000)
    test_command_id: str | None = None


class RepairStateRequest(BaseModel):
    confirm: bool = False
    target_status: str


class RecoveryEvent(BaseModel):
    id: str
    run_type: RunType
    run_id: str
    event_type: RecoveryEventType
    status_before: str | None = None
    status_after: str | None = None
    action_request_id: str | None = None
    source_step_id: str | None = None
    forked_from_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str


class RecoverySummary(BaseModel):
    run_type: RunType
    run_id: str
    status: str
    recoverability: str
    explanation: str
    pending_action: dict[str, Any] | None = None
    last_successful_step: dict[str, Any] | None = None
    last_failed_or_interrupted_step: dict[str, Any] | None = None
    forked_from_run_id: str | None = None
    forks: list[dict[str, Any]] = Field(default_factory=list)
    events: list[RecoveryEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class RecoveryRunDetail(BaseModel):
    summary: RecoverySummary
    detail: dict[str, Any]
