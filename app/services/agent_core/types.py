"""Contracts shared by the agent core.

The loop, the permission resolver, the tool registry and the transport layer all
speak these types. Keeping them in one module is what lets the loop stay ignorant
of whether a tool call arrived as a native ``tool_calls`` array or was parsed out
of a fenced JSON block by the text fallback.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

PermissionMode = Literal["plan", "normal", "auto"]

# A run ends for exactly one of these reasons. ``verified_complete`` and
# ``unverified_complete`` are deliberately distinct: a model that stops emitting
# tool calls has stopped talking, which is not the same as having finished the
# job, so the two cases are never collapsed into a single "success".
StopReason = Literal[
    "verified_complete",
    "unverified_complete",
    "blocked",
    "failed",
    "cancelled",
    "budget_exhausted",
]

SessionStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]

MessageRole = Literal["system", "user", "assistant", "tool"]

ToolOutcome = Literal["ok", "error", "denied", "proposed"]

TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running", "waiting_approval"})

# Stop reasons that leave the session in a non-failed terminal state.
_STATUS_FOR_STOP: dict[str, SessionStatus] = {
    "verified_complete": "completed",
    "unverified_complete": "completed",
    "blocked": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "budget_exhausted": "completed",
}


def status_for_stop_reason(reason: str) -> SessionStatus:
    return _STATUS_FOR_STOP.get(reason, "failed")


class ToolCall(BaseModel):
    """One model-requested invocation.

    ``id`` is assigned by the provider when it supports native tool calling and
    synthesised by the text fallback otherwise, so the loop can always correlate
    a result back to its request.
    """

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Evidence(BaseModel):
    """A checkable outcome produced by a tool.

    Completion adjudication reads these; without at least one passing row a run
    can only ever reach ``unverified_complete``.
    """

    kind: Literal["tests", "command", "file_verify", "patch_validate"]
    tool_call_id: str
    passed: bool
    detail: str = ""


class ToolResult(BaseModel):
    call_id: str
    name: str
    status: ToolOutcome = "ok"
    content: str = ""
    error: str | None = None
    duration_ms: int = 0
    evidence: Evidence | None = None

    @property
    def failed(self) -> bool:
        return self.status in {"error", "denied"}


class TodoItem(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    status: Literal["pending", "in_progress", "completed"] = "pending"


class AgentMessage(BaseModel):
    id: str
    session_id: str
    seq: int
    role: MessageRole
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    created_at: str


class Budgets(BaseModel):
    """Safety ceilings, not control flow.

    The loop stops because the model is finished, not because it ran out of
    iterations; these exist so a model that never converges cannot run forever.
    """

    max_iterations: int = Field(default=40, ge=1, le=500)
    max_seconds: int = Field(default=1200, ge=10, le=86_400)
    max_consecutive_errors: int = Field(default=5, ge=1, le=50)
    max_tool_calls: int = Field(default=120, ge=1, le=2000)
    context_fraction: float = Field(default=0.75, gt=0.0, le=1.0)


class AgentSession(BaseModel):
    id: str
    objective: str
    title: str
    status: SessionStatus = "queued"
    mode: PermissionMode = "normal"
    stop_reason: StopReason | None = None
    project_id: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    agent_definition_id: str | None = None
    agent_definition_snapshot: dict[str, Any] | None = None
    todo: list[TodoItem] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    budgets: Budgets = Field(default_factory=Budgets)
    iterations: int = 0
    tool_call_count: int = 0
    consecutive_errors: int = 0
    adjudications: int = 0
    summary: str | None = None
    error: str | None = None
    client_request_id: str | None = None
    worker_id: str | None = None
    lease_token: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    heartbeat_at: str | None = None


class PendingApproval(BaseModel):
    """A tool call frozen at the moment approval was requested.

    ``arguments_hash`` is what makes an approval single-use: the executor
    re-hashes the arguments it is about to run and refuses if they moved.
    """

    id: str
    session_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    arguments_hash: str
    reason: str
    grantable: bool = False
    status: Literal["pending", "approved", "rejected", "consumed"] = "pending"
    created_at: str
    decided_at: str | None = None


class Grant(BaseModel):
    """A capability-scoped session grant.

    Never a blank cheque: ``predicate`` is re-evaluated against the actual
    arguments of every later call, so a grant can only ever satisfy the approval
    step for invocations that still match what the user agreed to.
    """

    id: str
    session_id: str
    tool_name: str
    predicate: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class StopVerdict(BaseModel):
    stop_reason: StopReason
    summary: str
    continue_working: bool = False
    unmet_criteria: list[str] = Field(default_factory=list)
