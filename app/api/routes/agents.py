from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.agents import (
    AgentArtifact,
    AgentRun,
    AgentRunCreate,
    AgentsService,
    AgentStep,
    SaveRunToNoteRequest,
)
from app.services.agents.planner import AgentPlannerValidationError, AgentTaskPlanningService
from app.services.agents.service import AgentsValidationError
from app.services.agents.types import (
    AgentTaskPlan,
    ApprovalRequest,
    PlanTasksRequest,
    PlanTasksResult,
    RunFromObjectiveRequest,
)
from app.services.agentic_core import AgenticCoreService
from app.services.agentic_core import store as agentic_store
from app.services.notes.types import Note
from app.services.tasks.service import TasksValidationError
from app.services.tasks.types import Task
from app.services.tools.audit import calls_for_run
from app.services.tools.types import ToolCall

router = APIRouter(prefix="/agents", tags=["agents"])
task_router = APIRouter(prefix="/tasks", tags=["agents"])


class RunResponse(BaseModel):
    run: AgentRun


class RunsResponse(BaseModel):
    runs: list[AgentRun]
    total: int


class RunReadResponse(BaseModel):
    run: AgentRun
    steps: list[AgentStep]
    artifacts: list[AgentArtifact]
    tool_calls: list[ToolCall] = []
    agentic: dict | None = None


class StepResponse(BaseModel):
    step: AgentStep


class SaveNoteResponse(BaseModel):
    note: Note
    already_saved: bool


class RunFromObjectiveResponse(BaseModel):
    run: AgentRun
    parent_task: Task
    subtasks: list[Task]
    plan: AgentTaskPlan


def _service() -> AgentsService:
    return AgentsService()


@router.post("/plan-tasks", response_model=PlanTasksResult)
def plan_tasks(payload: PlanTasksRequest):
    try:
        return AgentTaskPlanningService().plan_tasks(payload)
    except (AgentPlannerValidationError, TasksValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/runs/from-objective", response_model=RunFromObjectiveResponse)
def run_from_objective(payload: RunFromObjectiveRequest):
    try:
        run, parent, subtasks, plan = AgentTaskPlanningService().run_from_objective(payload)
        return RunFromObjectiveResponse(run=run, parent_task=parent, subtasks=subtasks, plan=plan)
    except (AgentPlannerValidationError, TasksValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/runs", response_model=RunResponse)
def start_run(payload: AgentRunCreate):
    try:
        return RunResponse(run=_service().create_run(payload))
    except AgentsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs", response_model=RunsResponse)
def list_runs(
    task_id: str | None = None,
    project_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    try:
        runs, total = _service().list_runs(
            task_id=task_id, project_id=project_id, status=status, limit=limit, offset=offset
        )
        return RunsResponse(runs=runs, total=total)
    except AgentsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/runs/{run_id}", response_model=RunReadResponse)
def read_run(run_id: str):
    result = _service().read_run(run_id)
    if result is None:
        raise HTTPException(404, "Agent run not found.")
    run, steps, artifacts = result
    return RunReadResponse(
        run=run,
        steps=steps,
        artifacts=artifacts,
        tool_calls=[ToolCall(**item) for item in calls_for_run(run_id=run_id)],
        agentic=(
            AgenticCoreService().detail(linked["id"])
            if (linked := agentic_store.find_by_source("task", run_id))
            else None
        ),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run(run_id: str):
    run = _service().cancel_run(run_id)
    if run is None:
        raise HTTPException(404, "Agent run not found.")
    return RunResponse(run=run)


@router.post("/runs/{run_id}/steps/{step_id}/approve", response_model=StepResponse)
def approve_step(run_id: str, step_id: str, payload: ApprovalRequest):
    try:
        return StepResponse(step=_service().approve_step(run_id, step_id, payload.approved))
    except AgentsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/runs/{run_id}/save-to-note", response_model=SaveNoteResponse)
def save_to_note(run_id: str, payload: SaveRunToNoteRequest | None = None):
    try:
        note, already_saved = _service().save_output_to_note(
            run_id, payload or SaveRunToNoteRequest()
        )
        return SaveNoteResponse(note=note, already_saved=already_saved)
    except AgentsValidationError as exc:
        raise HTTPException(400, str(exc)) from exc


@task_router.get("/{task_id}/agent-runs", response_model=RunsResponse)
def task_runs(task_id: str):
    runs, total = _service().list_runs(task_id=task_id, limit=100)
    return RunsResponse(runs=runs, total=total)
