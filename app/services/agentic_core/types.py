from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

AgenticPhase = Literal[
    "PLAN",
    "INSPECT",
    "ACT",
    "VERIFY",
    "REFLECT",
    "CONTINUE",
    "DONE",
    "BLOCKED",
]
AgenticRunType = Literal["coding", "research", "task"]
AgenticFinalStatus = Literal["done", "blocked", "needs_user"]


class AgenticPlanStep(BaseModel):
    step_index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=2000)
    phase: AgenticPhase
    action_class: str
    required_context: list[str] = Field(default_factory=list)
    likely_tools: list[str] = Field(default_factory=list)
    verification_method: str
    risk_notes: list[str] = Field(default_factory=list)


class AgenticRunCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=20_000)
    run_type: AgenticRunType
    project_id: str | None = None
    task_id: str | None = None
    repo_id: str | None = None
    source_run_id: str | None = None
    max_steps: int = Field(default=20, ge=1, le=100)
    require_approval_for_actions: bool = True

    @field_validator("objective")
    @classmethod
    def clean_objective(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Objective is required.")
        return cleaned


class AgenticPlanUpdate(BaseModel):
    plan: list[AgenticPlanStep]
    completion_criteria: list[str] = Field(default_factory=list)


class AgenticStepRequest(BaseModel):
    action: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class AgenticContinueRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AgenticState(BaseModel):
    objective: str
    plan: list[dict[str, Any]] = Field(default_factory=list)
    current_step: str | None = None
    current_step_index: int = 0
    current_phase: AgenticPhase = "PLAN"
    known_context: list[dict[str, Any]] = Field(default_factory=list)
    tool_choices: list[dict[str, Any]] = Field(default_factory=list)
    actions_taken: list[dict[str, Any]] = Field(default_factory=list)
    verification_results: list[dict[str, Any]] = Field(default_factory=list)
    failures: list[dict[str, Any]] = Field(default_factory=list)
    recovery_attempts: list[dict[str, Any]] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str | None = None
    final_status: AgenticFinalStatus | None = None
    max_steps: int = 20
    require_approval_for_actions: bool = True
    project_id: str | None = None
    task_id: str | None = None
    repo_id: str | None = None
    context_budget: dict[str, Any] = Field(default_factory=dict)
    memory_retrieval_id: str | None = None
    memory_items_used: list[str] = Field(default_factory=list)
