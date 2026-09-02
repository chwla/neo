"""Session lifecycle: create, read, decide, cancel, deliver, export.

The API layer talks to this; it owns nothing itself beyond composing the store,
the worker and the permission model into the operations a caller needs.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.services.agent_core import delivery, journal, store, worker
from app.services.agent_core.permissions import grant_matches
from app.services.agent_core.types import (
    AgentSession,
    Budgets,
    Grant,
    PermissionMode,
    ToolCall,
)
from app.services.repos import store as repos_store

MAX_OBJECTIVE = 20_000


class AgentCoreValidationError(ValueError):
    pass


class SessionCreate(BaseModel):
    objective: str = Field(min_length=1, max_length=MAX_OBJECTIVE)
    mode: PermissionMode = "normal"
    project_id: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    chat_id: int | None = None
    anchor_message_id: int | None = None
    agent_definition_id: str | None = None
    disabled_tools: list[str] = Field(default_factory=list)
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


def _title(objective: str) -> str:
    first = objective.strip().splitlines()[0].strip()
    return (first[:117] + "...") if len(first) > 120 else first or "Agent session"


def _session(row: dict) -> AgentSession:
    return AgentSession(**{**row, "budgets": Budgets(**(row.get("budgets") or {}))})


class AgentCoreService:
    def create(self, payload: SessionCreate, *, start: bool = True) -> AgentSession:
        """Create a run, and by default hand it a worker.

        ``start=False`` writes the session and leaves it ``queued`` for the
        concurrency cap to release.  The row is identical either way, so nothing
        downstream has to know which happened -- the turn is simply waiting, and
        the same pump that starts a waiting plain turn starts this one.
        """

        objective = payload.objective.strip()
        if not objective:
            raise AgentCoreValidationError("An objective is required.")
        # A run without a repository is allowed: the registry withholds the tools
        # that need one, leaving search, fetch, recall and the checklist. Refusing
        # instead would make the agent unreachable in any conversation that is not
        # about a folder, which is the opposite of one thread doing both.

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

        # Every run is a turn of a conversation. A caller that has one -- the
        # composer -- passes it in; a task or the CLI does not, so one is opened
        # here. Without this a run could still be created that no chat contains,
        # which is precisely the orphan the unified thread removed.
        chat_id, anchor_message_id = payload.chat_id, payload.anchor_message_id
        if not chat_id:
            try:
                chat_id, anchor_message_id = store.create_chat_for_session(
                    objective, _title(objective)
                )
            except Exception:
                # A run is still better than no run; it simply will not be
                # reachable from the sidebar until the chat store recovers.
                chat_id, anchor_message_id = None, None

        now = store.now_iso()
        session_id = store.new_id()
        if anchor_message_id and not payload.anchor_message_id:
            store.set_anchor_session(anchor_message_id, session_id)
        row = store.insert_session(
            {
                "id": session_id,
                "objective": objective[:MAX_OBJECTIVE],
                "title": _title(objective),
                "status": "queued",
                "mode": payload.mode,
                "project_id": payload.project_id,
                "repo_id": payload.repo_id,
                "task_id": payload.task_id,
                "chat_id": chat_id,
                "anchor_message_id": anchor_message_id,
                "agent_definition_id": payload.agent_definition_id,
                "agent_definition_snapshot": snapshot,
                "disabled_tools": payload.disabled_tools,
                "budgets": Budgets().model_dump(),
                "client_request_id": payload.client_request_id,
                "created_at": now,
                "updated_at": now,
            }
        )
        if start:
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
        """What the run changed, and what the user can still do about it.

        The two workspace kinds answer "what changed" from different sources --
        a live run from its journal, a managed one from the import baseline --
        but they answer in the same shape, so the UI branches on ``mode`` alone
        and never infers the kind from a path.
        """

        if not session.repo_id or session.status != "completed":
            return None
        try:
            repo = repos_store.get_repo(session.repo_id)
            if repo is None:
                return None
            if repo.get("access") == repos_store.LIVE:
                plan = journal.session_changes(session.id, session.repo_id)
                return {
                    "mode": delivery.LIVE,
                    "root": repo["original_path"],
                    "deliverable": [
                        {"path": change.relative_path, "status": change.status}
                        for change in plan.changes
                    ],
                    "blocked": [],
                    "undoable": bool(plan.changes),
                }
            plan = delivery.plan_delivery(session.repo_id)
        except Exception:
            return None
        return {
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
            if chosen.get("kind") == "path_prefix" and not str(chosen.get("value") or ""):
                # An empty prefix matches nothing by design, so the generic
                # message would blame the user's scope for a missing one.
                raise AgentCoreValidationError(
                    "A path scope is required. This file sits at the repository root, "
                    "so grant the whole repository or type a folder to narrow it to."
                )
            raise AgentCoreValidationError("That grant would not cover the action being approved.")
        store.insert_grant(grant.model_dump())

    def deliver(self, session_id: str, payload: DeliverRequest) -> dict:
        session = self.get(session_id)
        if not session.repo_id:
            raise AgentCoreValidationError("This session has no repository.")
        repo = repos_store.get_repo(session.repo_id)
        if repo is not None and repo.get("access") == repos_store.LIVE:
            # A live run has nothing to move, but "show me what changed" is still
            # a real question, so the diff is served from the journal and only
            # the write-back mode is refused.
            plan = journal.session_changes(session_id, session.repo_id)
            if payload.mode != "patch":
                raise AgentCoreValidationError(
                    "This workspace is your own folder, so these changes are already in it."
                )
            return {
                "mode": "patch",
                "patch": delivery.build_patch(plan, payload.files),
                "summary": delivery.summarize(plan),
                "delivery_mode": delivery.LIVE,
            }
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
                "This workspace has no folder on this machine to write into."
            )
        result = delivery.write_to_working_tree(plan, payload.files)
        return {
            "mode": "working_tree",
            "summary": delivery.summarize(plan),
            "delivery_mode": plan.mode,
            **result,
        }

    def undo(self, session_id: str) -> dict:
        """Reverse what this run wrote into the user's folder."""

        session = self.get(session_id)
        if not session.repo_id:
            raise AgentCoreValidationError("This session has no repository.")
        repo = repos_store.get_repo(session.repo_id)
        if repo is None or repo.get("access") != repos_store.LIVE:
            raise AgentCoreValidationError(
                "Only a run against a folder on this machine can be undone."
            )
        return journal.undo(session_id, session.repo_id)

    def clone_for_fork(
        self, session_id: str, *, chat_id: int, anchor_message_id: int
    ) -> AgentSession:
        """Copy a finished run into a new chat's transcript.

        Forking a conversation needs the run to look and behave exactly like the
        original from its own new home, not merely describe it -- so this clones
        the session row, its tool calls and its file snapshots (the diff and
        undo journal) rather than referencing the original. Cloning the
        snapshots is safe even though both copies now point at the same
        repository: ``journal.undo`` drift-checks a file's content against
        ``after_sha256`` before touching it, so whichever copy undoes first wins
        and the other's later attempt is skipped rather than double-applied.

        Events, approvals and grants are deliberately left behind -- a finished
        session no worker will ever resume again has no use for them, and the
        trace replays from ``tool_calls`` alone once there are no live events
        (see ``AgentTurn.traceEntries`` on the frontend).
        """

        original = self.get(session_id)
        new_id = store.new_id()
        store.insert_session(
            {
                "id": new_id,
                "objective": original.objective,
                "title": original.title,
                "mode": original.mode,
                "project_id": original.project_id,
                "repo_id": original.repo_id,
                "task_id": original.task_id,
                "agent_definition_id": original.agent_definition_id,
                "agent_definition_snapshot": original.agent_definition_snapshot,
                "disabled_tools": original.disabled_tools,
                "chat_id": chat_id,
                "anchor_message_id": anchor_message_id,
                "todo": [item.model_dump() for item in original.todo],
                "evidence": [item.model_dump() for item in original.evidence],
                "budgets": original.budgets.model_dump(),
                "client_request_id": None,
                "created_at": original.created_at,
                "updated_at": original.created_at,
            }
        )
        # insert_session's column list has no room for a run's outcome -- it is
        # shaped for a session that is only just starting. Backfilling these
        # separately is what makes the clone read as already finished rather
        # than as a fresh run silently stuck at "queued".
        store.update_session(
            new_id,
            {
                "status": original.status,
                "stop_reason": original.stop_reason,
                "iterations": original.iterations,
                "tool_call_count": original.tool_call_count,
                "consecutive_errors": original.consecutive_errors,
                "adjudications": original.adjudications,
                "summary": original.summary,
                "error": original.error,
                "started_at": original.started_at,
                "completed_at": original.completed_at,
            },
        )
        for row in store.list_tool_calls(session_id):
            store.record_tool_call(
                new_id,
                {
                    "call_id": row["call_id"],
                    "name": row["tool_name"],
                    "arguments": row["arguments"],
                    "status": row["status"],
                    "content": row["content"],
                    "error": row["error"],
                    "duration_ms": row["duration_ms"],
                },
            )
        for row in store.list_file_snapshots(session_id):
            store.insert_file_snapshot(
                {
                    "id": store.new_id(),
                    "session_id": new_id,
                    "repo_id": row["repo_id"],
                    "relative_path": row["relative_path"],
                    "existed_before": row["existed_before"],
                    "before_text": row["before_text"],
                    "before_newline": row["before_newline"],
                    "after_sha256": row["after_sha256"],
                    "created_at": row["created_at"],
                }
            )
        return self.get(new_id)

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
