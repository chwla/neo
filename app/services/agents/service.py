from __future__ import annotations

import uuid

import app.services.agents.store as store
from app.services.agents.runner import get_agent_runner
from app.services.agents.types import (
    AgentArtifact,
    AgentRun,
    AgentRunCreate,
    AgentStep,
    SaveRunToNoteRequest,
)
from app.services.notes import NoteCreate, NotesService
from app.services.projects import ProjectsService
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest
from app.services.tasks import TasksService

ALLOWED_RUN_STATUSES = {
    "queued",
    "planning",
    "running",
    "waiting_approval",
    "completed",
        "failed",
        "cancelled",
        "interrupted",
        "needs_review",
}


class AgentsValidationError(ValueError):
    pass


class AgentsService:
    def __init__(self, runner=None) -> None:
        self.runner = runner or get_agent_runner()

    def create_run(self, payload: AgentRunCreate) -> AgentRun:
        task = TasksService().get_task(payload.task_id)
        if task is None:
            raise AgentsValidationError("Task not found.")
        if payload.mode != "assist":
            raise AgentsValidationError("Agent Runner v1 supports assist mode only.")
        objective = (payload.objective or task.description or task.title).strip()
        if not objective:
            raise AgentsValidationError("Run objective is required.")
        now = store.now_iso()
        rule_result = RuleResolver().resolve(
            RuleResolveRequest(
                context_type="agent",
                context_id=None,
                project_id=task.project_id,
                task_id=task.id,
            )
        )
        run = store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "task_id": task.id,
                "project_id": task.project_id,
                "title": f"Agent run: {task.title}"[:200],
                "objective": objective[:10000],
                "status": "queued",
                "mode": "assist",
                "plan": [],
                "final_output": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "cancelled_at": None,
                "forked_from_run_id": None,
            }
        )
        rule_snapshot = {
            "resolved_rules": rule_result["resolved_rules"],
            "applied_profiles": rule_result["applied_profiles"],
            "warnings": rule_result["warnings"],
        }
        self._create_initial_step(
            run["id"], 0, "read_context", "Read task context", input_data={"rules": rule_snapshot}
        )
        self._create_initial_step(run["id"], 1, "plan", "Create a safe plan")
        self.runner.start(run["id"])
        return AgentRun(**run)

    def list_runs(self, **filters) -> tuple[list[AgentRun], int]:
        status = filters.get("status")
        if status and status not in ALLOWED_RUN_STATUSES:
            raise AgentsValidationError("Invalid agent run status.")
        rows, total = store.list_runs(
            task_id=filters.get("task_id"),
            project_id=filters.get("project_id"),
            status=status,
            limit=max(1, min(int(filters.get("limit", 50)), 100)),
            offset=max(0, int(filters.get("offset", 0))),
        )
        return [AgentRun(**row) for row in rows], total

    def read_run(self, run_id: str) -> tuple[AgentRun, list[AgentStep], list[AgentArtifact]] | None:
        run = store.get_run(run_id)
        if run is None:
            return None
        return (
            AgentRun(**run),
            [AgentStep(**step) for step in store.list_steps(run_id)],
            [AgentArtifact(**artifact) for artifact in store.list_artifacts(run_id)],
        )

    def cancel_run(self, run_id: str) -> AgentRun | None:
        run = store.cancel_run(run_id)
        return AgentRun(**run) if run else None

    def approve_step(self, run_id: str, step_id: str, approved: bool) -> AgentStep:
        run = store.get_run(run_id)
        step = store.get_step(step_id)
        if run is None or step is None or step["run_id"] != run_id:
            raise AgentsValidationError("Agent run or step not found.")
        if not step["requires_approval"]:
            raise AgentsValidationError("This step does not require approval.")
        status = "completed" if approved else "skipped"
        output = (
            "Approved; no external action was executed in Agent Runner v1."
            if approved
            else "User denied this action."
        )
        updated = store.update_step(
            step_id,
            {
                "status": status,
                "approval_status": "approved" if approved else "denied",
                "output_text": output,
                "completed_at": store.now_iso(),
            },
        )
        store.update_run(run_id, {"status": "running"})
        self.runner.start(run_id)
        return AgentStep(**updated)

    def save_output_to_note(self, run_id: str, payload: SaveRunToNoteRequest):
        run = store.get_run(run_id)
        if run is None:
            raise AgentsValidationError("Agent run not found.")
        if run["status"] != "completed" or not run.get("final_output"):
            raise AgentsValidationError("Only completed runs with final output can be saved.")
        notes = NotesService()
        existing = notes.find_by_source("agent_run", run_id)
        already_saved = existing is not None
        note = existing or notes.create_note(
            NoteCreate(
                title=payload.title or run["title"],
                body=run["final_output"],
                tags=payload.tags,
                summary=f"Output from Agent Runner for task {run['task_id']}",
                source_type="agent_run",
                source_id=run_id,
                source_title=run["title"],
                source_metadata={"task_id": run["task_id"], "project_id": run.get("project_id")},
            )
        )
        TasksService().attach_note(run["task_id"], note.id)
        if run.get("project_id"):
            ProjectsService().attach_note(run["project_id"], note.id)
        artifacts = store.list_artifacts(run_id)
        if not any(item.get("note_id") == note.id for item in artifacts):
            store.insert_artifact(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "artifact_type": "note",
                    "title": note.title,
                    "content": None,
                    "note_id": note.id,
                    "task_id": run["task_id"],
                    "project_id": run.get("project_id"),
                    "metadata": {"source_type": "agent_run"},
                    "created_at": store.now_iso(),
                }
            )
        return note, already_saved

    def _create_initial_step(
        self,
        run_id: str,
        index: int,
        step_type: str,
        title: str,
        input_data: dict | None = None,
    ) -> None:
        now = store.now_iso()
        store.insert_step(
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "step_index": index,
                "step_type": step_type,
                "title": title,
                "status": "pending",
                "input": input_data or {},
                "output_text": None,
                "error": None,
                "requires_approval": False,
                "approval_status": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
            }
        )
