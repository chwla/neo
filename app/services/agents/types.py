from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.tasks.types import Task, TaskPriority, TaskStatus

RunStatus = Literal["queued", "planning", "running", "waiting_approval", "completed", "failed", "cancelled"]
StepStatus = Literal["pending", "running", "waiting_approval", "completed", "failed", "skipped", "cancelled"]
StepType = Literal[
    "plan", "read_context", "think", "web_search", "research", "draft",
    "summarize", "save_note", "task_update_request", "final",
]


class AgentRun(BaseModel):
    id: str
    task_id: str
    project_id: str | None = None
    title: str
    objective: str
    status: RunStatus = "queued"
    mode: str = "assist"
    plan: list[dict[str, Any]] = Field(default_factory=list)
    final_output: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None


class AgentStep(BaseModel):
    id: str
    run_id: str
    step_index: int
    step_type: StepType
    title: str
    status: StepStatus = "pending"
    input: dict[str, Any] = Field(default_factory=dict)
    output_text: str | None = None
    error: str | None = None
    requires_approval: bool = False
    approval_status: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    completed_at: str | None = None


class AgentArtifact(BaseModel):
    id: str
    run_id: str
    artifact_type: str
    title: str
    content: str | None = None
    note_id: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class AgentRunCreate(BaseModel):
    task_id: str
    objective: str | None = None
    mode: str = "assist"


class SaveRunToNoteRequest(BaseModel):
    title: str | None = None
    tags: list[str] = Field(default_factory=lambda: ["agent", "task-output"])


class ApprovalRequest(BaseModel):
    approved: bool


class PlannedTask(BaseModel):
    title: str
    description: str
    priority: TaskPriority = "medium"
    status: TaskStatus = "todo"
    project_id: str | None = None
    parent_task_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    order: int | None = None


class AgentTaskPlan(BaseModel):
    objective: str
    project_id: str | None = None
    parent_task: PlannedTask
    subtasks: list[PlannedTask]
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class PlanTasksRequest(BaseModel):
    objective: str
    project_id: str | None = None
    dry_run: bool = True


class PlanTasksResult(BaseModel):
    plan: AgentTaskPlan
    created: bool
    tasks: list[Task] = Field(default_factory=list)


class RunFromObjectiveRequest(BaseModel):
    objective: str
    project_id: str | None = None
    mode: str = "assist"
    auto_create_tasks: bool = True
