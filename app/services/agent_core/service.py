"""Session lifecycle: create, read, decide, cancel, deliver, export.

The API layer talks to this; it owns nothing itself beyond composing the store,
the worker and the permission model into the operations a caller needs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.services.agent_core import delivery, store, worker
from app.services.agent_core.permissions import grant_matches
from app.services.agent_core.types import (
    AgentSession,
    Budgets,
    Grant,
    PermissionMode,
    ToolCall,
)

MAX_OBJECTIVE = 20_000


class AgentCoreValidationError(ValueError):
    pass


class SessionCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE)
    mode: PermissionMode = "normal"
    project_id: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    agent_definition_id: str | None = None
    client_request_id: str | None = Field(default=None, max_length=200)


class ApprovalDecision(BaseModel):
    decision: str = Field(pattern="^(allow_once|allow_always|reject)$")
    #: Only meaningful with allow_always: the constraint the user is agreeing to.
    predicate: dict[str, Any] | None = None


class SessionUpdate(BaseModel):
    mode: PermissionMode


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)


class DeliverRequest(BaseModel):
    mode: str = Field(default="patch", pattern="^(patch|working_tree)$")
    files: list[str] | None = None


def _safe_filename(raw: str) -> str:
    """A download name that cannot smuggle a path or a quote into the header."""

    cleaned = "".join(char if char.isalnum() or char in "-_" else "-" for char in raw.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return (cleaned[:60] or "agent-run").lower()


def _title(objective: str) -> str:
    first = objective.strip().splitlines()[0].strip()
    return (first[:117] + "...") if len(first) > 120 else first or "Agent session"


def _session(row: dict) -> AgentSession:
    return AgentSession(**{**row, "budgets": Budgets(**(row.get("budgets") or {}))})


class AgentCoreService:
    def create(self, payload: SessionCreate) -> AgentSession:
        objective = payload.objective.strip()
        if not objective:
            raise AgentCoreValidationError("An objective is required.")
        if not payload.repo_id:
            # Without a repository the agent has no file or command tools at all,
            # so it can only narrate work it cannot do. Refusing here is kinder
            # than a run that looks busy and changes nothing.
            raise AgentCoreValidationError(
                "Select a repository first. Upload a folder to give the agent "
                "code to work on."
            )

        if payload.client_request_id:
            # Idempotent submit: a retried POST must not start a second run.
            existing = store.get_session_by_request(payload.client_request_id)
            if existing:
                return _session(existing)

        snapshot = None
        if payload.agent_definition_id:
            try:
                from app.services.agent_framework.service import AgentDefinitionService

                snapshot = (
                    AgentDefinitionService()
                    .resolve_for_run(payload.agent_definition_id)
                    .model_dump()
                )
            except Exception:
                snapshot = None

        now = store.now_iso()
        row = store.insert_session(
            {
                "id": store.new_id(),
                "objective": objective[:MAX_OBJECTIVE],
                "title": _title(objective),
                "status": "queued",
                "mode": payload.mode,
                "project_id": payload.project_id,
                "repo_id": payload.repo_id,
                "task_id": payload.task_id,
                "agent_definition_id": payload.agent_definition_id,
                "agent_definition_snapshot": snapshot,
                "budgets": Budgets().model_dump(),
                "client_request_id": payload.client_request_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        worker.start(row["id"])
        return _session(row)

    def get(self, session_id: str) -> AgentSession:
        row = store.get_session(session_id)
        if row is None:
            raise LookupError("Agent session not found.")
        return _session(row)

    def detail(self, session_id: str) -> dict:
        session = self.get(session_id)
        return {
            "session": session.model_dump(),
            "messages": [
                message
                for message in store.list_messages(session_id)
                if message["role"] != "system"
            ],
            "tool_calls": store.list_tool_calls(session_id),
            "pending_approval": store.pending_approval(session_id),
            "grants": store.list_grants(session_id),
            "delivery": self._delivery_summary(session),
        }

    @staticmethod
    def _delivery_summary(session: AgentSession) -> dict | None:
        if not session.repo_id or session.status != "completed":
            return None
        try:
            plan = delivery.plan_delivery(session.repo_id)
        except Exception:
            return None
        return {
            # The UI offers write-back or download on this alone, so it is the
            # server that decides, not a guess made from the path in the browser.
            "mode": plan.mode,
            "deliverable": [
                {"path": change.relative_path, "status": change.status}
                for change in plan.deliverable
            ],
            "blocked": [
                {"path": change.relative_path, "reason": change.reason} for change in plan.blocked
            ],
        }

    def list(self, *, limit: int = 25, task_id: str | None = None) -> list[AgentSession]:
        return [
            _session(row)
            for row in store.list_sessions(limit=max(1, min(limit, 100)), task_id=task_id)
        ]

    def events(self, session_id: str, after: int = 0, limit: int = 500) -> list[dict]:
        self.get(session_id)
        return store.list_events(session_id, after=after, limit=limit)

    def cancel(self, session_id: str) -> AgentSession:
        self.get(session_id)
        store.cancel_session(session_id)
        return self.get(session_id)

    def set_mode(self, session_id: str, payload: SessionUpdate) -> AgentSession:
        session = self.get(session_id)
        if session.status in {"completed", "failed", "cancelled"}:
            raise AgentCoreValidationError("This run has already finished.")
        store.update_session(session_id, {"mode": payload.mode})
        return self.get(session_id)

    def add_message(self, session_id: str, payload: MessageCreate) -> AgentSession:
        """Steer a run mid-flight.

        The message is appended to the transcript the next iteration reads, so a
        correction lands before the agent's next decision rather than after it.
        """

        session = self.get(session_id)
        if session.status in {"completed", "failed", "cancelled"}:
            raise AgentCoreValidationError("This run has already finished.")
        store.append_message(session_id, {"role": "user", "content": payload.content.strip()})
        # A session waiting on approval must not be restarted here: the loop would
        # resume without executing the approved call, silently dropping it. The
        # message is queued and the approval decision picks it up.
        if session.status == "running" and not worker.is_active(session_id):
            worker.start(session_id)
        return self.get(session_id)

    def decide(self, session_id: str, approval_id: str, payload: ApprovalDecision) -> AgentSession:
        self.get(session_id)  # 404s for an unknown session before touching approvals
        approval = store.get_approval(approval_id)
        if approval is None or approval["session_id"] != session_id:
            raise LookupError("Approval not found.")
        if approval["status"] != "pending":
            raise AgentCoreValidationError("That approval has already been decided.")

        if payload.decision == "allow_always":
            if not approval["grantable"]:
                raise AgentCoreValidationError(
                    "This action cannot be granted for the session; approve it individually."
                )
            self._create_grant(session_id, approval, payload.predicate)

        decided = store.decide_approval(approval_id, payload.decision != "reject")
        if decided is None:
            raise AgentCoreValidationError("That approval has already been decided.")

        worker.start(session_id, approval_id=approval_id)
        return self.get(session_id)

    @staticmethod
    def _create_grant(session_id: str, approval: dict, predicate: dict | None) -> None:
        """Record a session grant, refusing one that would not cover this very call.

        A predicate that does not match the arguments in front of the user is
        either a mistake or an attempt to widen the grant, and both are caught by
        checking it against the call being approved.
        """

        from app.services.agent_core.tools.registry import ToolRegistry

        tool = ToolRegistry().get(approval["tool_name"])
        if tool is None:
            raise AgentCoreValidationError("Unknown tool.")
        chosen = predicate or {"kind": "exact_arguments", "value": approval["arguments"]}
        grant = Grant(
            id=store.new_id(),
            session_id=session_id,
            tool_name=approval["tool_name"],
            predicate=chosen,
            created_at=store.now_iso(),
        )
        if not grant_matches(grant, tool, approval["arguments"]):
            raise AgentCoreValidationError("That grant would not cover the action being approved.")
        store.insert_grant(grant.model_dump())

    def deliver(self, session_id: str, payload: DeliverRequest) -> dict:
        session = self.get(session_id)
        if not session.repo_id:
            raise AgentCoreValidationError("This session has no repository.")
        plan = delivery.plan_delivery(session.repo_id)
        if payload.mode == "patch":
            return {
                "mode": "patch",
                "patch": delivery.build_patch(plan, payload.files),
                "summary": delivery.summarize(plan),
                "delivery_mode": plan.mode,
            }
        # `write_to_working_tree` refuses this too; raising here turns it into a
        # 400 with an actionable message rather than a generic workspace error.
        if not plan.writable:
            raise AgentCoreValidationError(
                "This repository was uploaded, so there is no folder on this machine "
                "to write into. Download the changed files instead."
            )
        result = delivery.write_to_working_tree(plan, payload.files)
        return {
            "mode": "working_tree",
            "summary": delivery.summarize(plan),
            "delivery_mode": plan.mode,
            **result,
        }

    def download(self, session_id: str, scope: str = "changes") -> tuple[bytes, str]:
        """Zip the work for a repository Neo cannot write back to."""

        session = self.get(session_id)
        if not session.repo_id:
            raise AgentCoreValidationError("This session has no repository.")
        plan = delivery.plan_delivery(session.repo_id)
        whole = scope == "workspace"
        if not whole and not plan.deliverable:
            raise AgentCoreValidationError("No files changed in the managed copy.")
        archive = delivery.build_archive(plan, whole_workspace=whole)
        suffix = "workspace" if whole else "changes"
        return archive, f"{_safe_filename(session.title)}-{suffix}.zip"

    def export(self, session_id: str, target: str) -> dict:
        """Promote a finished run into Tasks or a Note, only when asked."""

        session = self.get(session_id)
        body = session.summary or "The run produced no summary."
        if target == "note":
            from app.services.notes.service import NotesService
            from app.services.notes.types import NoteCreate

            note = NotesService().create_note(
                NoteCreate(
                    title=session.title,
                    body=f"{session.objective}\n\n---\n\n{body}",
                    tags=["agent", "session-output"],
                    source_type="agent_run",
                    source_id=session.id,
                )
            )
            return {
                "target": "note",
                "note": note.model_dump() if hasattr(note, "model_dump") else note,
            }

        from app.services.tasks import TaskCreate, TasksService

        service = TasksService()
        parent = service.create_task(
            TaskCreate(
                title=session.title,
                description=f"{session.objective}\n\n{body}",
                status="done" if session.stop_reason == "verified_complete" else "doing",
                project_id=session.project_id,
                tags=["agent", "session-export"],
            )
        )
        created = [parent]
        for item in session.todo:
            created.append(
                service.create_task(
                    TaskCreate(
                        title=item.title,
                        status="done" if item.status == "completed" else "todo",
                        project_id=session.project_id,
                        parent_task_id=parent.id,
                        tags=["agent", "session-export"],
                    )
                )
            )
        return {"target": "tasks", "tasks": [task.model_dump() for task in created]}


__all__ = [
    "AgentCoreService",
    "AgentCoreValidationError",
    "ApprovalDecision",
    "DeliverRequest",
    "MessageCreate",
    "SessionCreate",
    "SessionUpdate",
    "ToolCall",
]
