from __future__ import annotations

import uuid
from typing import Any

import app.services.agents.store as agent_store
import app.services.coding_agent.store as coding_store
import app.services.recovery.store as store
import app.services.test_runner.store as test_store
from app.services.agents.runner import get_agent_runner
from app.services.chat_intent import is_internal_chat_command
from app.services.coding_agent.orchestrator import CodingAgentOrchestrator
from app.services.coding_agent.types import CodingRunCreate
from app.services.recovery.safety import (
    CODING_WAITING_STATUSES,
    require_confirm,
    validate_repair_target,
    validate_run_type,
)
from app.services.recovery.scanner import RecoveryScanner
from app.services.recovery.types import RecoveryEvent, RecoverySummary


class RecoveryValidationError(ValueError):
    pass


class RecoveryService:
    def __init__(self, *, runner=None, coding_orchestrator=None) -> None:
        store.initialize_recovery_tables()
        self.runner = runner or get_agent_runner()
        self.coding = coding_orchestrator or CodingAgentOrchestrator()

    def scan(self) -> dict:
        return RecoveryScanner().scan()

    def list_runs(self, *, run_type: str | None = None, limit: int = 100) -> dict:
        if run_type:
            validate_run_type(run_type)
        items: list[RecoverySummary] = []
        if run_type in (None, "agent"):
            runs, _ = agent_store.list_runs(limit=limit)
            items.extend(self.summary("agent", item["id"]) for item in runs)
        if run_type in (None, "coding_agent"):
            runs, _ = coding_store.list_runs(limit=limit)
            items.extend(self.summary("coding_agent", item["id"]) for item in runs)
        items.sort(key=lambda item: item.events[0].created_at if item.events else "", reverse=True)
        return {"runs": items[:limit], "total": len(items)}

    def detail(self, run_type: str, run_id: str) -> dict:
        validate_run_type(run_type)
        summary = self.summary(run_type, run_id)
        if run_type == "agent":
            run = agent_store.get_run(run_id)
            if not run:
                raise RecoveryValidationError("Agent run not found.")
            detail = {
                "run": run,
                "steps": agent_store.list_steps(run_id),
                "artifacts": agent_store.list_artifacts(run_id),
            }
        else:
            detail = self.coding.detail(run_id)
        return {"summary": summary, "detail": detail}

    def summary(self, run_type: str, run_id: str) -> RecoverySummary:
        validate_run_type(run_type)
        if run_type == "agent":
            run = agent_store.get_run(run_id)
            if not run:
                raise RecoveryValidationError("Agent run not found.")
            steps = agent_store.list_steps(run_id)
            pending = next((s for s in reversed(steps) if s["status"] == "waiting_approval"), None)
            forks, _ = agent_store.list_runs(limit=500)
            forks = [
                {"id": item["id"], "status": item["status"], "title": item["title"]}
                for item in forks
                if item.get("forked_from_run_id") == run_id
            ]
        else:
            run = coding_store.get_run(run_id)
            if not run:
                raise RecoveryValidationError("Coding-agent run not found.")
            steps = agent_store.list_steps(run["agent_run_id"])
            pending = next(
                (
                    a
                    for a in reversed(coding_store.list_actions(run_id))
                    if a["status"] == "pending"
                ),
                None,
            )
            forks, _ = coding_store.list_runs(limit=500)
            forks = [
                {"id": item["id"], "status": item["status"], "objective": item["objective"]}
                for item in forks
                if item.get("forked_from_run_id") == run_id
            ]
        events, _ = store.list_events(run_type=run_type, run_id=run_id, limit=50)
        status = run["status"]
        recoverability, explanation = self._recoverability(run_type, run, pending)
        return RecoverySummary(
            run_type=run_type,
            run_id=run_id,
            status=status,
            recoverability=recoverability,
            explanation=explanation,
            pending_action=pending,
            last_successful_step=next(
                (s for s in reversed(steps) if s["status"] == "completed"), None
            ),
            last_failed_or_interrupted_step=next(
                (
                    s
                    for s in reversed(steps)
                    if s["status"] in {"failed", "interrupted", "needs_review"}
                ),
                None,
            ),
            forked_from_run_id=run.get("forked_from_run_id"),
            forks=forks,
            events=[RecoveryEvent(**item) for item in events],
            warnings=[
                "Resume never applies patches, runs tests, or creates checkpoints without approval.",
                "Fork creates a new run; it does not modify the original run.",
            ],
        )

    def resume(self, run_type: str, run_id: str, *, confirm: bool) -> dict:
        require_confirm(confirm, "resume this run")
        validate_run_type(run_type)
        summary = self.summary(run_type, run_id)
        if summary.status == "cancelled":
            raise RecoveryValidationError("Cancelled runs cannot resume; fork instead.")
        if summary.status == "completed":
            raise RecoveryValidationError("Completed runs are read-only; fork instead.")
        if summary.status in {"failed", "needs_review"}:
            raise RecoveryValidationError("This run needs retry, repair, or fork before resume.")
        if summary.status in {"waiting_approval", *CODING_WAITING_STATUSES}:
            store.insert_event(
                run_type=run_type,
                run_id=run_id,
                event_type="resumed",
                status_before=summary.status,
                status_after=summary.status,
                action_request_id=(summary.pending_action or {}).get("id"),
                metadata={"result": "pending approval preserved; no action executed"},
            )
            return self.detail(run_type, run_id)
        if run_type == "agent" and summary.status == "interrupted":
            agent_store.update_run(run_id, {"status": "queued", "error": None})
            store.insert_event(
                run_type=run_type,
                run_id=run_id,
                event_type="resumed",
                status_before="interrupted",
                status_after="queued",
                metadata={"result": "agent runner restarted after explicit confirmation"},
            )
            self.runner.start(run_id)
            return self.detail(run_type, run_id)
        raise RecoveryValidationError("This run cannot be resumed from its current state.")

    def retry(self, run_type: str, run_id: str, *, confirm: bool, **options) -> dict:
        require_confirm(confirm, "retry this run")
        validate_run_type(run_type)
        if run_type == "agent":
            run = agent_store.get_run(run_id)
            if not run:
                raise RecoveryValidationError("Agent run not found.")
            if run["status"] not in {"failed", "interrupted"}:
                raise RecoveryValidationError("Only failed or interrupted agent runs can retry.")
            agent_store.update_run(run_id, {"status": "queued", "error": None})
            store.insert_event(
                run_type="agent",
                run_id=run_id,
                event_type="retry_requested",
                status_before=run["status"],
                status_after="queued",
                metadata={"safe_action": "agent runner restarted; approvals still required"},
            )
            self.runner.start(run_id)
            return self.detail("agent", run_id)
        run = coding_store.get_run(run_id)
        if not run:
            raise RecoveryValidationError("Coding-agent run not found.")
        if run["status"] in {"completed", "cancelled"}:
            raise RecoveryValidationError("This run cannot retry; fork instead.")
        actions = coding_store.list_actions(run_id)
        pending = next((a for a in reversed(actions) if a["status"] == "pending"), None)
        if pending and pending["action_type"] in {"apply_patch", "run_tests", "create_checkpoint"}:
            raise RecoveryValidationError(
                "Run is waiting for approval; resume shows the same gate."
            )
        if run.get("test_run_id"):
            test_run = test_store.get_run(run["test_run_id"])
            if test_run and test_run["status"] in {"failed", "error", "timed_out", "interrupted"}:
                command = (
                    test_store.get_command(test_run["test_command_id"])
                    if test_run.get("test_command_id")
                    else None
                )
                if not command:
                    raise RecoveryValidationError(
                        "The failed test has no saved command to approve for retry."
                    )
                coding_store.update_run(
                    run_id,
                    {
                        "status": "waiting_test_approval",
                        "recovery_state": "retry_waiting_approval",
                        "last_recoverable_at": coding_store.now_iso(),
                        "updated_at": coding_store.now_iso(),
                    },
                )
                action = self._create_coding_action(
                    run,
                    "run_tests",
                    "Retry saved test",
                    "Reruns only the previously approved saved command after explicit approval.",
                    {
                        "test_commands": [
                            {
                                "id": command["id"],
                                "name": command["name"],
                                "command": command["command"],
                            }
                        ],
                        "retry_of_test_run_id": test_run["id"],
                    },
                )
                store.insert_event(
                    run_type="coding_agent",
                    run_id=run_id,
                    event_type="retry_requested",
                    status_before=run["status"],
                    status_after="waiting_test_approval",
                    action_request_id=action["id"],
                    metadata={"retry": "test_run", "test_run_id": test_run["id"]},
                )
                return self.detail("coding_agent", run_id)
        failed_checkpoint = next(
            (
                action
                for action in reversed(actions)
                if action["action_type"] == "create_checkpoint" and action["status"] == "failed"
            ),
            None,
        )
        if failed_checkpoint:
            coding_store.update_run(
                run_id,
                {
                    "status": "waiting_checkpoint_approval",
                    "recovery_state": "retry_waiting_approval",
                    "last_recoverable_at": coding_store.now_iso(),
                    "updated_at": coding_store.now_iso(),
                },
            )
            action = self._create_coding_action(
                run,
                "create_checkpoint",
                "Retry local checkpoint",
                "Creates a local managed-workspace checkpoint only after explicit approval.",
                {"retry_of_action_request_id": failed_checkpoint["id"]},
            )
            store.insert_event(
                run_type="coding_agent",
                run_id=run_id,
                event_type="retry_requested",
                status_before=run["status"],
                status_after="waiting_checkpoint_approval",
                action_request_id=action["id"],
                metadata={"retry": "checkpoint", "failed_action_id": failed_checkpoint["id"]},
            )
            return self.detail("coding_agent", run_id)
        if run["status"] in {"interrupted", "failed", "waiting_patch_approval"}:
            instructions = (
                options.get("instructions") or "Retry proposal generation after recovery."
            )
            before = run["status"]
            if run["status"] != "waiting_patch_approval":
                coding_store.update_run(run_id, {"status": "waiting_patch_approval"})
            store.insert_event(
                run_type="coding_agent",
                run_id=run_id,
                event_type="retry_requested",
                status_before=before,
                status_after="waiting_patch_approval",
                metadata={"retry": "patch_proposal"},
            )
            detail = self.coding.revise(run_id, instructions)
            store.insert_event(
                run_type="coding_agent",
                run_id=run_id,
                event_type="retry_completed",
                status_before="waiting_patch_approval",
                status_after=detail["coding_run"]["status"],
                metadata={"result": "proposal regenerated; approval still required"},
            )
            return self.detail("coding_agent", run_id)
        raise RecoveryValidationError("No safe retry is available for this state.")

    def fork(self, run_type: str, run_id: str, *, confirm: bool, **options) -> dict:
        require_confirm(confirm, "fork this run")
        validate_run_type(run_type)
        objective_override = (options.get("objective_override") or "").strip() or None
        if run_type == "agent":
            run = agent_store.get_run(run_id)
            if not run:
                raise RecoveryValidationError("Agent run not found.")
            now = agent_store.now_iso()
            fork = agent_store.insert_run(
                {
                    "id": str(uuid.uuid4()),
                    "task_id": run["task_id"],
                    "project_id": run.get("project_id"),
                    "title": f"Fork: {run['title']}"[:200],
                    "objective": (objective_override or run["objective"])[:10000],
                    "status": "queued",
                    "mode": run.get("mode", "assist"),
                    "plan": [],
                    "final_output": None,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                    "started_at": None,
                    "completed_at": None,
                    "cancelled_at": None,
                    "forked_from_run_id": run_id,
                    "agent_definition_id": run.get("agent_definition_id"),
                    "agent_definition_snapshot": run.get("agent_definition_snapshot"),
                }
            )
            self._seed_agent_fork_steps(fork["id"])
            store.insert_event(
                run_type="agent",
                run_id=fork["id"],
                event_type="fork_created",
                forked_from_run_id=run_id,
                source_step_id=options.get("from_step_id"),
                metadata={"source_run_id": run_id},
            )
            return self.detail("agent", fork["id"])
        run = coding_store.get_run(run_id)
        if not run:
            raise RecoveryValidationError("Coding-agent run not found.")
        detail = self.coding.start(
            CodingRunCreate(
                objective=objective_override or run["objective"],
                task_id=run.get("task_id"),
                project_id=run.get("project_id"),
                repo_id=run.get("repo_id"),
                max_iterations=run.get("max_iterations", 3),
                agent_definition_id=run.get("agent_definition_id"),
            )
        )
        fork_id = detail["coding_run"]["id"]
        coding_store.update_run(
            fork_id,
            {
                "forked_from_run_id": run_id,
                "selected_files": run.get("selected_files", []),
                "updated_at": coding_store.now_iso(),
            },
        )
        store.insert_event(
            run_type="coding_agent",
            run_id=fork_id,
            event_type="fork_created",
            forked_from_run_id=run_id,
            action_request_id=options.get("from_action_request_id"),
            source_step_id=options.get("from_step_id"),
            metadata={"source_run_id": run_id, "approvals_copied": False},
        )
        return self.detail("coding_agent", fork_id)

    def repair_state(
        self, run_type: str, run_id: str, *, confirm: bool, target_status: str
    ) -> dict:
        require_confirm(confirm, "repair this run state")
        validate_repair_target(run_type, target_status)
        before = self.summary(run_type, run_id).status
        if run_type == "agent":
            agent_store.update_run(run_id, {"status": target_status})
        else:
            coding_store.update_run(
                run_id,
                {
                    "status": target_status,
                    "recovery_state": "manual_repair",
                    "last_recoverable_at": coding_store.now_iso(),
                    "updated_at": coding_store.now_iso(),
                },
            )
        store.insert_event(
            run_type=run_type,
            run_id=run_id,
            event_type="state_repaired",
            status_before=before,
            status_after=target_status,
            metadata={"manual": True},
        )
        return self.detail(run_type, run_id)

    def list_events(self, **filters) -> dict:
        events, total = store.list_events(
            run_type=filters.get("run_type"),
            run_id=filters.get("run_id"),
            limit=max(1, min(int(filters.get("limit", 100)), 500)),
            offset=max(0, int(filters.get("offset", 0))),
        )
        return {"recovery_events": events, "total": total}

    def answer_for_prompt(self, prompt: str) -> str | None:
        if not is_internal_chat_command(prompt, "recovery"):
            return None
        candidates: list[RecoverySummary] = []
        for run_type in ("coding_agent", "agent"):
            try:
                data = self.list_runs(run_type=run_type, limit=5)
                candidates.extend(data["runs"])
            except Exception:
                continue
        if not candidates:
            return "No recoverable agent or coding-agent runs were found."
        summary = candidates[0]
        pending = summary.pending_action
        waiting = pending.get("title") if pending else "no pending approval"
        forks = ", ".join(item.get("id", "") for item in summary.forks) or "none"
        stopped = (
            summary.last_failed_or_interrupted_step.get("title")
            if summary.last_failed_or_interrupted_step
            else summary.explanation
        )
        return (
            f"Latest {summary.run_type} run {summary.run_id} is {summary.status}. "
            f"Recoverability: {summary.recoverability}. Waiting for: {waiting}. "
            f"Why it stopped / current state: {stopped}. Forks: {forks}. "
            "This is read-only; chat cannot resume, retry, fork, approve, apply patches, "
            "run tests, or create checkpoints."
        )

    @staticmethod
    def _seed_agent_fork_steps(run_id: str) -> None:
        now = agent_store.now_iso()
        for index, (step_type, title) in enumerate(
            (("read_context", "Read task context"), ("plan", "Create a safe plan"))
        ):
            agent_store.insert_step(
                {
                    "id": str(uuid.uuid4()),
                    "run_id": run_id,
                    "step_index": index,
                    "step_type": step_type,
                    "title": title,
                    "status": "pending",
                    "input": {},
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

    @staticmethod
    def _create_coding_action(
        run: dict, action_type: str, title: str, description: str, payload: dict
    ) -> dict:
        now = coding_store.now_iso()
        return coding_store.insert_action(
            {
                "id": str(uuid.uuid4()),
                "coding_run_id": run["id"],
                "agent_run_id": run["agent_run_id"],
                "action_type": action_type,
                "status": "pending",
                "title": title,
                "description": description,
                "payload": payload,
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "decided_at": None,
                "executed_at": None,
            }
        )

    @staticmethod
    def _recoverability(
        run_type: str, run: dict[str, Any], pending: dict | None
    ) -> tuple[str, str]:
        status = run["status"]
        if status == "completed":
            return "fork_only", "Completed runs are read-only unless forked."
        if status == "cancelled":
            return "fork_only", "Cancelled runs cannot resume; fork to continue differently."
        if pending or status in {"waiting_approval", *CODING_WAITING_STATUSES}:
            return (
                "resumable",
                "Run is waiting for explicit approval; resume preserves the same gate.",
            )
        if status == "failed":
            return "retry_or_fork", "Failed runs can retry a safe failed step or fork."
        if status in {"interrupted", "needs_review"}:
            return (
                "needs_review",
                "Interrupted state requires retry, repair, or fork; no action is automatic.",
            )
        if status in {"queued", "planning", "running"}:
            return (
                "scanner_needed",
                "Run appears active; scanner can mark it recoverable after restart.",
            )
        return "unknown", f"{run_type} run status is {status}."
