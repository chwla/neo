from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from app.services.agentic_core import store
from app.services.agentic_core.context_budget import ContextBudgetManager
from app.services.agentic_core.executor import AgenticExecutor
from app.services.agentic_core.planner import AgenticPlanner
from app.services.agentic_core.policy import SAFETY_CONTEXT, AgenticPolicy
from app.services.agentic_core.reflector import AgenticReflector
from app.services.agentic_core.types import (
    AgenticPlanUpdate,
    AgenticRunCreate,
    AgenticState,
    AgenticStepRequest,
)
from app.services.agentic_core.verifier import AgenticVerifier

SAFE_SOURCE_ERRORS = (LookupError, RuntimeError, ValueError, sqlite3.Error)


class AgenticCoreError(ValueError):
    pass


class AgenticCoreService:
    def __init__(
        self,
        *,
        planner: AgenticPlanner | None = None,
        executor: AgenticExecutor | None = None,
        verifier: AgenticVerifier | None = None,
        reflector: AgenticReflector | None = None,
        context_budget: ContextBudgetManager | None = None,
    ) -> None:
        store.initialize_agentic_core_tables()
        self.planner = planner or AgenticPlanner()
        self.executor = executor or AgenticExecutor()
        self.verifier = verifier or AgenticVerifier()
        self.reflector = reflector or AgenticReflector()
        self.context_budget = context_budget or ContextBudgetManager()

    def start(self, request: AgenticRunCreate, *, auto_plan: bool = True) -> dict[str, Any]:
        now = store.now_iso()
        state = AgenticState(
            objective=request.objective,
            max_steps=request.max_steps,
            require_approval_for_actions=request.require_approval_for_actions,
            project_id=request.project_id,
            task_id=request.task_id,
            repo_id=request.repo_id,
            next_action="Create and review the plan.",
        ).model_dump()
        run = store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "run_type": request.run_type,
                "source_run_id": request.source_run_id,
                "objective": request.objective,
                "status": "planning",
                "state": state,
                "plan": [],
                "completion_criteria": [],
                "context_budget": {},
                "final_report": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
        )
        self.context(run["id"])
        return self.plan(run["id"]) if auto_plan else self.detail(run["id"])

    def plan(self, run_id: str, override: AgenticPlanUpdate | None = None) -> dict[str, Any]:
        run = self._run(run_id)
        if run["status"] in {"done", "stopped"}:
            raise AgenticCoreError("A terminal run cannot be replanned.")
        state = run["state"]
        if override is None:
            plan, criteria = self.planner.create_plan(
                run["objective"], run["run_type"], run.get("context_budget") or {}
            )
        else:
            plan = [item.model_dump() for item in override.plan]
            criteria = list(override.completion_criteria)
        if not plan:
            raise AgenticCoreError("Agentic plan must contain at least one step.")
        for index, item in enumerate(plan):
            item["step_index"] = index
            decision = AgenticPolicy().decide(
                item.get("action_class", ""),
                require_approval=bool(state.get("require_approval_for_actions", True)),
            )
            if not decision.allowed:
                raise AgenticCoreError(f"Plan step {index + 1}: {decision.reason}")
        state.update(
            {
                "plan": plan,
                "completion_criteria": criteria,
                "current_step_index": 0,
                "current_step": plan[0]["title"],
                "current_phase": "INSPECT",
                "final_status": None,
                "blockers": [],
                "next_action": f"Run step 1: {plan[0]['title']}",
            }
        )
        store.update_run(
            run_id,
            {
                "status": "running",
                "state": state,
                "plan": plan,
                "completion_criteria": criteria,
            },
        )
        self._insert_step(
            run_id,
            phase="PLAN",
            title="Create agentic plan",
            status="completed",
            output={
                "plan": plan,
                "completion_criteria": criteria,
                "risk_notes": [note for item in plan for note in item.get("risk_notes", [])],
            },
            verification={
                "expected_outcome": "A bounded ordered plan with verification and risks.",
                "actual_outcome": f"Created {len(plan)} plan step(s).",
                "passed": True,
                "evidence": [f"Plan persisted for run {run_id}."],
                "next_action": state["next_action"],
            },
            reflection={
                "what_changed": "The run now has an editable persisted plan.",
                "what_was_learned": [f"Run type: {run['run_type']}."],
                "completion_criteria_satisfied": False,
                "user_input_required": False,
                "recommended_next_step": state["next_action"],
            },
        )
        return self.detail(run_id)

    def execute_step(
        self, run_id: str, request: AgenticStepRequest | None = None
    ) -> dict[str, Any]:
        run = self._run(run_id)
        state = run["state"]
        if run["status"] in {"done", "stopped"} or state.get("current_phase") == "DONE":
            raise AgenticCoreError("Agentic run is already complete.")
        if state.get("current_phase") == "BLOCKED":
            raise AgenticCoreError("Resolve the recorded blocker before continuing.")
        plan = state.get("plan") or run.get("plan") or []
        index = int(state.get("current_step_index", 0))
        if index >= len(plan):
            return self._complete(run_id)
        action_steps = [step for step in store.list_steps(run_id) if step["phase"] != "PLAN"]
        if len(action_steps) >= int(state.get("max_steps", 20)):
            return self._block(
                run_id,
                "Maximum step limit reached before completion.",
                "Review progress and start a bounded follow-up run if more work is required.",
            )
        plan_step = plan[index]
        payload = (request or AgenticStepRequest()).model_dump()
        attempts = 0
        while True:
            try:
                result = self.executor.execute(plan_step, run, state, payload)
                break
            except Exception as exc:  # provider/tool boundaries must fail safely
                failure = {
                    "step_index": index,
                    "action_class": plan_step.get("action_class"),
                    "error": str(exc),
                    "recorded_at": store.now_iso(),
                }
                state.setdefault("failures", []).append(failure)
                if attempts == 0 and AgenticPolicy.can_retry(plan_step.get("action_class", "")):
                    attempts += 1
                    state.setdefault("recovery_attempts", []).append(
                        {
                            "step_index": index,
                            "strategy": "retry_safe_read_only_action",
                            "reason": str(exc),
                            "recorded_at": store.now_iso(),
                        }
                    )
                    continue
                result = {
                    "status": "blocked",
                    "summary": "The action failed safely.",
                    "error": str(exc),
                    "blocker": f"Action provider/tool failure: {exc}",
                    "evidence": ["Failure was caught and no unsafe fallback executed."],
                    "next_action": "Repair the unavailable provider/tool or ask the user.",
                }
                break

        verification = self.verifier.verify(plan_step, result)
        reflection = self.reflector.reflect(plan_step, result, verification, state)
        action_record = {
            "step_index": index,
            "title": plan_step["title"],
            "action_class": payload.get("action") or plan_step.get("action_class"),
            "status": result.get("status"),
            "summary": result.get("summary"),
            "approval_reference": result.get("approval_reference"),
            "recorded_at": store.now_iso(),
        }
        state.setdefault("actions_taken", []).append(action_record)
        state.setdefault("tool_choices", []).extend(result.get("tool_choices") or [])
        state.setdefault("verification_results", []).append({"step_index": index, **verification})
        if result.get("delegation_result"):
            state.setdefault("known_context", []).append(
                {
                    "kind": "subagent_result",
                    "step_index": index,
                    "result": result["delegation_result"],
                }
            )
        self._insert_step(
            run_id,
            phase=plan_step.get("phase", "ACT"),
            title=plan_step["title"],
            status="blocked"
            if result.get("blocker")
            else ("completed" if verification.get("passed") else "failed"),
            input=payload,
            output=result,
            tool_calls=result.get("tool_choices") or [],
            verification=verification,
            reflection=reflection,
            error=result.get("error"),
        )
        if result.get("blocker"):
            state.setdefault("blockers", []).append(
                {
                    "step_index": index,
                    "message": result["blocker"],
                    "approval_reference": result.get("approval_reference"),
                    "action_class": action_record["action_class"],
                    "recorded_at": store.now_iso(),
                }
            )
            state["current_phase"] = "BLOCKED"
            state["final_status"] = "needs_user" if result.get("requires_approval") else "blocked"
            state["next_action"] = result.get("next_action")
            store.update_run(run_id, {"status": "blocked", "state": state})
            return self.detail(run_id)
        if not verification.get("passed"):
            return self._block(
                run_id,
                result.get("error") or "Verification failed.",
                verification.get("next_action") or "Revise the plan.",
                state=state,
            )

        state["current_step_index"] = index + 1
        if state["current_step_index"] >= len(plan):
            store.update_run(run_id, {"state": state})
            return self._complete(run_id)
        next_step = plan[state["current_step_index"]]
        state.update(
            {
                "current_phase": "CONTINUE",
                "current_step": next_step["title"],
                "next_action": (
                    f"Continue with step {state['current_step_index'] + 1}: {next_step['title']}"
                ),
            }
        )
        store.update_run(run_id, {"status": "running", "state": state})
        return self.detail(run_id)

    def continue_run(self, run_id: str, note: str | None = None) -> dict[str, Any]:
        run = self._run(run_id)
        state = run["state"]
        if run["status"] == "done":
            return self.detail(run_id)
        if state.get("current_phase") == "BLOCKED":
            blocker = (state.get("blockers") or [{}])[-1]
            resolved, evidence = self._approval_resolved(run, blocker)
            state.setdefault("recovery_attempts", []).append(
                {
                    "step_index": blocker.get("step_index"),
                    "strategy": "inspect_persisted_approval_state",
                    "resolved": resolved,
                    "evidence": evidence,
                    "note": note,
                    "recorded_at": store.now_iso(),
                }
            )
            if not resolved:
                state["next_action"] = blocker.get("message")
                store.update_run(run_id, {"state": state})
                return self.detail(run_id)
            state["blockers"] = state.get("blockers", [])[:-1]
            state["current_step_index"] = int(state.get("current_step_index", 0)) + 1
            state["current_phase"] = "CONTINUE"
            state["final_status"] = None
            state["next_action"] = "Continue after verified approval outcome."
            store.update_run(run_id, {"status": "running", "state": state})
        return self.execute_step(run_id)

    def reflect(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        steps = store.list_steps(run_id)
        if not steps:
            raise AgenticCoreError("No step exists to reflect on.")
        last = steps[-1]
        reflection = self.reflector.reflect(
            {"step_index": last["step_index"], "title": last["title"]},
            last.get("output") or {},
            last.get("verification") or {},
            run["state"],
        )
        self._insert_step(
            run_id,
            phase="REFLECT",
            title=f"Reflect on {last['title']}",
            status="completed",
            input={"source_step_id": last["id"]},
            output={"reflection": reflection},
            verification={
                "expected_outcome": "Reflection cites an actual persisted step.",
                "actual_outcome": f"Reflected on step {last['id']}.",
                "passed": True,
                "evidence": [last["id"]],
                "next_action": reflection.get("recommended_next_step"),
            },
            reflection=reflection,
        )
        return self.detail(run_id)

    def stop(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        if run["status"] == "done":
            return self.detail(run_id)
        state = run["state"]
        state.update(
            {
                "current_phase": "BLOCKED",
                "final_status": "blocked",
                "next_action": "Run stopped by user; persisted evidence remains available.",
            }
        )
        state.setdefault("blockers", []).append(
            {"message": "Run stopped by user.", "recorded_at": store.now_iso()}
        )
        store.update_run(
            run_id, {"status": "stopped", "state": state, "completed_at": store.now_iso()}
        )
        return self.detail(run_id)

    def context(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        items = self._context_items(run)
        budget = self.context_budget.assemble(items)
        state = run["state"]
        state["context_budget"] = budget
        state["known_context"] = [
            {
                "kind": item["kind"],
                "source_id": item.get("source_id"),
                "estimated_tokens": item["estimated_tokens"],
            }
            for item in budget["included_items"]
        ]
        store.update_run(run_id, {"state": state, "context_budget": budget})
        return budget

    def detail(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        return {
            "agentic_run": run,
            "state": run["state"],
            "plan": run["plan"],
            "completion_criteria": run["completion_criteria"],
            "context_budget": run["context_budget"],
            "steps": store.list_steps(run_id),
            "blockers": run["state"].get("blockers", []),
            "pending_approvals": [
                item for item in run["state"].get("blockers", []) if item.get("approval_reference")
            ],
            "final_report": run.get("final_report"),
        }

    def list(self, **filters: Any) -> dict[str, Any]:
        runs, total = store.list_runs(
            status=filters.get("status"),
            run_type=filters.get("run_type"),
            limit=max(1, min(int(filters.get("limit", 50)), 200)),
            offset=max(0, int(filters.get("offset", 0))),
        )
        return {"agentic_runs": runs, "total": total}

    def record_external_step(
        self,
        *,
        run_type: str,
        source_run_id: str,
        phase: str,
        title: str,
        status: str,
        output: str | None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        run = store.find_by_source(run_type, source_run_id)
        if not run:
            return None
        normalized_phase = (
            phase
            if phase
            in {"PLAN", "INSPECT", "ACT", "VERIFY", "REFLECT", "CONTINUE", "DONE", "BLOCKED"}
            else "ACT"
        )
        passed = status == "completed" and bool(output)
        verification = {
            "expected_outcome": f"Persisted {run_type} workflow step completes with actual output.",
            "actual_outcome": output or error or f"Step status: {status}.",
            "passed": passed,
            "evidence": [output] if output else ([error] if error else []),
            "next_action": "Reflect on the external workflow result.",
        }
        reflection = {
            "what_changed": output or "No verified change recorded.",
            "what_was_learned": verification["evidence"],
            "what_failed": error,
            "plan_should_change": status == "failed",
            "more_context_needed": False,
            "user_input_required": status in {"waiting_approval", "blocked"},
            "completion_criteria_satisfied": normalized_phase == "DONE" and passed,
            "recommended_next_step": verification["next_action"],
            "grounded_in_external_run": source_run_id,
        }
        self._insert_step(
            run["id"],
            phase=normalized_phase,
            title=title,
            status=status,
            output={"summary": output, "evidence": verification["evidence"]},
            verification=verification,
            reflection=reflection,
            error=error,
        )
        state = run["state"]
        state.setdefault("actions_taken", []).append(
            {
                "title": title,
                "status": status,
                "source_run_id": source_run_id,
                "recorded_at": store.now_iso(),
            }
        )
        state.setdefault("verification_results", []).append(verification)
        if status in {"waiting_approval", "blocked"}:
            state["current_phase"] = "BLOCKED"
            state["final_status"] = "needs_user"
            state["next_action"] = "Resolve the existing external approval gate."
            store.update_run(run["id"], {"status": "blocked", "state": state})
        elif normalized_phase == "DONE" and passed:
            state["current_phase"] = "DONE"
            state["final_status"] = "done"
            state["next_action"] = None
            store.update_run(
                run["id"],
                {
                    "status": "done",
                    "state": state,
                    "final_report": output,
                    "completed_at": store.now_iso(),
                },
            )
        else:
            state["current_phase"] = "CONTINUE"
            store.update_run(run["id"], {"status": "running", "state": state})
        return self.detail(run["id"])

    def _context_items(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        state = run["state"]
        items = [
            dict(SAFETY_CONTEXT),
            {
                "kind": "objective",
                "importance": 100,
                "required": True,
                "content": run["objective"],
            },
        ]
        task_id = state.get("task_id")
        project_id = state.get("project_id")
        repo_id = state.get("repo_id")
        try:
            from app.services.rules.resolver import RuleResolver
            from app.services.rules.types import RuleResolveRequest

            resolved = RuleResolver().resolve(
                RuleResolveRequest(
                    context_type="coding_agent" if run["run_type"] == "coding" else "agent",
                    project_id=project_id,
                    task_id=task_id,
                    repo_id=repo_id,
                    profile_ids=[],
                )
            )
            items.append(
                {
                    "kind": "rules",
                    "importance": 95,
                    "required": True,
                    "content": resolved.get("resolved_rules", {}),
                }
            )
        except SAFE_SOURCE_ERRORS as exc:
            items.append(
                {
                    "kind": "rules_fallback",
                    "importance": 95,
                    "required": True,
                    "content": (
                        f"Rule resolution unavailable; built-in safety remains active: {exc}"
                    ),
                }
            )
        scope = ("task", task_id) if task_id else (("project", project_id) if project_id else None)
        if scope:
            try:
                from app.services.context_memory import ContextMemoryService

                memory = ContextMemoryService().scope(*scope)
                items.append(
                    {
                        "kind": "memory_summary" if memory.get("used") else "memory_fallback",
                        "source_id": memory.get("summary_id"),
                        "importance": 80,
                        "content": memory.get("summary_text") or memory.get("reason"),
                    }
                )
            except SAFE_SOURCE_ERRORS as exc:
                items.append(
                    {
                        "kind": "memory_fallback",
                        "importance": 75,
                        "content": (
                            f"Context memory unavailable; source data remains authoritative: {exc}"
                        ),
                    }
                )
            try:
                from app.services.memory_retrieval import MemoryRetrievalService

                retrieved = MemoryRetrievalService().retrieve_for_agent(
                    run["objective"], scope_type=scope[0], scope_id=scope[1], source="agentic_core"
                )
                retrieval = retrieved.get("retrieval", {})
                state["memory_retrieval_id"] = retrieval.get("id")
                state["memory_items_used"] = [
                    item["memory_id"] for item in retrieved.get("results", [])
                ]
                if retrieved.get("results"):
                    items.append(
                        {
                            "kind": "retrieved_memory",
                            "source_id": retrieval.get("id"),
                            "importance": 88,
                            "content": [
                                {
                                    "title": item["title"],
                                    "snippet": item["snippet"],
                                    "memory_type": item["memory_type"],
                                }
                                for item in retrieved["results"]
                            ],
                        }
                    )
            except SAFE_SOURCE_ERRORS:
                # Retrieval supplements source data and safety; it is never authoritative.
                pass
        if repo_id:
            try:
                from app.services.repos import store as repo_store

                repo = repo_store.get_repo(repo_id)
                if repo:
                    items.append(
                        {
                            "kind": "repo",
                            "source_id": repo_id,
                            "importance": 85,
                            "content": {
                                "id": repo_id,
                                "name": repo.get("display_name") or repo.get("name"),
                                "status": repo.get("status"),
                                "project_id": repo.get("project_id"),
                            },
                        }
                    )
            except SAFE_SOURCE_ERRORS as exc:
                items.append({"kind": "repo_fallback", "importance": 70, "content": str(exc)})
            try:
                from app.services.lsp import LSPService

                lsp = LSPService().status()
                available = [
                    item["language"] for item in lsp.get("servers", []) if item["available"]
                ]
                items.append(
                    {
                        "kind": "lsp",
                        "importance": 78,
                        "content": (
                            {"available_languages": available, "degraded": False}
                            if available
                            else {
                                "available_languages": [],
                                "degraded": True,
                                "reason": (
                                    "No allowlisted LSP server is available; static symbols "
                                    "remain fallback."
                                ),
                            }
                        ),
                    }
                )
            except SAFE_SOURCE_ERRORS as exc:
                items.append(
                    {
                        "kind": "lsp_fallback",
                        "importance": 78,
                        "content": f"LSP unavailable; static symbols remain fallback: {exc}",
                    }
                )
        source = run.get("source_run_id")
        if source:
            items.append(
                {
                    "kind": "source_run",
                    "source_id": source,
                    "importance": 90,
                    "content": self._source_snapshot(run["run_type"], source),
                }
            )
        return items

    @staticmethod
    def _source_snapshot(run_type: str, source_run_id: str) -> dict[str, Any]:
        try:
            if run_type == "coding":
                from app.services.coding_agent import store as source_store
            else:
                from app.services.agents import store as source_store
            source = source_store.get_run(source_run_id)
            if not source:
                return {"status": "missing"}
            return {
                "id": source_run_id,
                "status": source.get("status"),
                "objective": source.get("objective"),
                "error": source.get("error"),
            }
        except SAFE_SOURCE_ERRORS as exc:
            return {"status": "unavailable", "reason": str(exc)}

    @staticmethod
    def _approval_resolved(run: dict[str, Any], blocker: dict[str, Any]) -> tuple[bool, list[str]]:
        reference = blocker.get("approval_reference")
        action = blocker.get("action_class")
        if not reference:
            return False, ["No persisted approval reference exists."]
        try:
            if action == "request_command":
                from app.services.command_sandbox import store as command_store

                item = command_store.get_run(reference)
                status = item.get("status") if item else "missing"
                return status in {"completed", "failed", "cancelled", "rejected"}, [
                    f"Command Sandbox {reference}: {status}."
                ]
            if run["run_type"] == "coding":
                from app.services.coding_agent import store as coding_store

                item = coding_store.get_action(reference)
                status = item.get("status") if item else "missing"
                return status in {"completed", "failed", "rejected"}, [
                    f"Coding action {reference}: {status}."
                ]
        except SAFE_SOURCE_ERRORS as exc:
            return False, [f"Approval state lookup failed safely: {exc}"]
        return False, ["The existing approval gate has not reached a terminal decision."]

    def _complete(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        state = run["state"]
        verified = state.get("verification_results", [])
        if not verified or not all(item.get("passed") for item in verified):
            return self._block(
                run_id,
                "Completion criteria cannot be claimed because verification is incomplete.",
                "Inspect failed or missing verification results.",
                state=state,
            )
        report = self._final_report(run, state)
        state.update(
            {
                "current_phase": "DONE",
                "current_step": None,
                "final_status": "done",
                "next_action": None,
            }
        )
        store.update_run(
            run_id,
            {
                "status": "done",
                "state": state,
                "final_report": report,
                "completed_at": store.now_iso(),
            },
        )
        self._insert_step(
            run_id,
            phase="DONE",
            title="Complete grounded final report",
            status="completed",
            output={"report": report},
            verification={
                "expected_outcome": "All recorded verification results pass.",
                "actual_outcome": f"Validated {len(verified)} verification record(s).",
                "passed": True,
                "evidence": [f"verification:{index}" for index in range(len(verified))],
                "next_action": None,
            },
            reflection={
                "what_changed": "The run reached DONE from persisted evidence.",
                "completion_criteria_satisfied": True,
                "user_input_required": False,
                "recommended_next_step": None,
            },
        )
        try:
            from app.services.memory_retrieval import MemoryRetrievalService

            MemoryRetrievalService().refresh_agentic_run(self._run(run_id))
        except SAFE_SOURCE_ERRORS:
            # Memory refresh is additive; completion evidence remains valid without it.
            pass
        return self.detail(run_id)

    def _block(
        self, run_id: str, message: str, next_action: str, *, state: dict | None = None
    ) -> dict[str, Any]:
        run = self._run(run_id)
        state = state or run["state"]
        state.setdefault("blockers", []).append(
            {"message": message, "recorded_at": store.now_iso()}
        )
        state.update(
            {
                "current_phase": "BLOCKED",
                "final_status": "blocked",
                "next_action": next_action,
            }
        )
        store.update_run(run_id, {"status": "blocked", "state": state})
        return self.detail(run_id)

    @staticmethod
    def _final_report(run: dict[str, Any], state: dict[str, Any]) -> str:
        lines = [
            f"Objective: {run['objective']}",
            f"Status: {state.get('final_status') or 'done'}",
            "",
            "Verified results:",
        ]
        for item in state.get("verification_results", []):
            lines.append(
                f"- Step {item.get('step_index', '?')}: "
                f"{'pass' if item.get('passed') else 'fail'} — {item.get('actual_outcome')}"
            )
            for evidence in item.get("evidence") or []:
                lines.append(f"  Evidence: {str(evidence)[:1200]}")
        if state.get("failures"):
            lines.append("\nFailures:")
            lines.extend(f"- {item.get('error')}" for item in state["failures"])
        lines.append(
            "\nApproval safety: no patch, command, test, checkpoint, or external write "
            "was bypassed."
        )
        return "\n".join(lines)

    def _insert_step(
        self,
        run_id: str,
        *,
        phase: str,
        title: str,
        status: str,
        input: dict[str, Any] | None = None,
        output: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        verification: dict[str, Any] | None = None,
        reflection: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        steps = store.list_steps(run_id)
        now = store.now_iso()
        return store.insert_step(
            {
                "id": str(uuid.uuid4()),
                "agentic_run_id": run_id,
                "step_index": len(steps),
                "phase": phase,
                "title": title,
                "status": status,
                "input": input or {},
                "output": output or {},
                "tool_calls": tool_calls or [],
                "verification": verification or {},
                "reflection": reflection or {},
                "error": error,
                "created_at": now,
                "completed_at": now if status != "running" else None,
            }
        )

    @staticmethod
    def _run(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if not run:
            raise LookupError("Agentic run not found.")
        return run
