from __future__ import annotations

import re

import app.services.coding_agent.store as store
from app.services.chat_intent import is_internal_chat_command
from app.services.coding_agent.orchestrator import CodingAgentOrchestrator
from app.services.coding_agent.types import CodingRunCreate
from app.services.command_sandbox import CommandSandboxService
from app.services.command_sandbox.types import CommandRequest

CODING_CONTEXT_INTENT = re.compile(
    r"\b(coding run|coding agent|patch applied|selected files|checkpoint created|"
    r"tests pass|tests fail)\b",
    re.I,
)


class CodingAgentService:
    def __init__(self, orchestrator: CodingAgentOrchestrator | None = None) -> None:
        self.orchestrator = orchestrator or CodingAgentOrchestrator()

    def start(self, request: CodingRunCreate) -> dict:
        return self.orchestrator.start(request)

    def read(self, run_id: str) -> dict:
        return self.orchestrator.detail(run_id)

    def list(self, **filters):
        return store.list_runs(**filters)

    def approve(self, action_id: str, confirm: bool, options: dict) -> dict:
        return self.orchestrator.approve(action_id, confirm=confirm, options=options)

    def reject(self, action_id: str, reason: str | None) -> dict:
        return self.orchestrator.reject(action_id, reason)

    def revise(self, run_id: str, instructions: str) -> dict:
        return self.orchestrator.revise(run_id, instructions)

    def cancel(self, run_id: str) -> dict:
        return self.orchestrator.cancel(run_id)

    def propose_command(self, run_id: str, command: list[str], category: str, reason: str) -> dict:
        run = store.get_run(run_id)
        if not run:
            raise LookupError("Coding-agent run not found.")
        proposal = CommandSandboxService().propose(
            CommandRequest(
                workspace_id=run["repo_id"],
                command=command,
                cwd=".",
                category=category,
                created_by=f"coding_agent:{run_id}",
            )
        )
        proposal["agent_reason"] = reason
        return proposal

    def context_for_prompt(self, prompt: str) -> str:
        if not CODING_CONTEXT_INTENT.search(prompt):
            return ""
        runs, _ = store.list_runs(limit=5)
        if not runs:
            return "Stored coding-agent context: no coding runs."
        return (
            "Stored coding-agent context (read-only; chat cannot approve actions):\n"
            + "\n".join(self._summary(item) for item in runs)
        )

    def answer_for_prompt(self, prompt: str) -> str | None:
        if not is_internal_chat_command(prompt, "coding"):
            return None
        runs, _ = store.list_runs(limit=1)
        if not runs:
            return (
                "There are no stored coding-agent runs. Chat cannot start or approve "
                "coding actions."
            )
        run = runs[0]
        pending = next(
            (
                item
                for item in reversed(store.list_actions(run["id"]))
                if item["status"] == "pending"
            ),
            None,
        )
        files = ", ".join(item["relative_path"] for item in run.get("selected_files", [])) or "none"
        waiting = pending["title"] if pending else "no pending approval"
        return (
            f"Latest coding run: {run['objective']} — {run['status']}. "
            f"Waiting for: {waiting}. Files considered: {files}. "
            "This answer is read-only; chat cannot approve or execute actions."
        )

    @staticmethod
    def _summary(run: dict) -> str:
        files = ", ".join(item["relative_path"] for item in run.get("selected_files", [])) or "none"
        return (
            f"- {run['objective']} [{run['status']}, iteration "
            f"{run['current_iteration']}/{run['max_iterations']}], files: {files}"
        )
