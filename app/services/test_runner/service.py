from __future__ import annotations

import re
import uuid

from app.services.agents import store as agent_store
from app.services.patch_apply import store as patch_store
from app.services.repos import store as repo_store
from app.services.tasks import store as task_store
from app.services.test_runner import store
from app.services.test_runner.detectors import detect_commands
from app.services.test_runner.executor import execute
from app.services.test_runner.safety import (
    resolve_working_directory,
    validate_command,
    validate_timeout,
)
from app.services.test_runner.types import TestCommandCreate, TestCommandUpdate, TestRunRequest

TEST_INTENT = re.compile(
    r"\b(test run|test history|tests? fail|failed tests?|patch pass|pass tests?)\b", re.I
)


class TestRunnerService:
    def _repo(self, repo_id: str) -> dict:
        repo = repo_store.get_repo(repo_id)
        if not repo:
            raise LookupError("Managed repository not found.")
        return repo

    def detect(self, repo_id: str) -> list[dict]:
        repo = self._repo(repo_id)
        root = resolve_working_directory(repo["workspace_path"], ".")
        return [item.model_dump() for item in detect_commands(root)]

    def create_command(self, repo_id: str, request: TestCommandCreate) -> dict:
        repo = self._repo(repo_id)
        command = validate_command(request.command)
        validate_timeout(request.timeout_seconds)
        resolve_working_directory(repo["workspace_path"], request.working_directory)
        project_id = request.project_id or repo.get("project_id")
        if request.project_id and request.project_id != repo.get("project_id"):
            raise ValueError("Test command project must match the repository project.")
        now = store.now_iso()
        return store.insert_command(
            {
                "id": str(uuid.uuid4()),
                "repo_id": repo_id,
                "project_id": project_id,
                "name": request.name.strip(),
                "command": command,
                "working_directory": request.working_directory,
                "timeout_seconds": request.timeout_seconds,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
        )

    def update_command(self, command_id: str, request: TestCommandUpdate) -> dict:
        command_item = store.get_command(command_id)
        if not command_item:
            raise LookupError("Test command not found.")
        repo = self._repo(command_item["repo_id"])
        updates = request.model_dump(exclude_unset=True)
        if "command" in updates:
            updates["command"] = validate_command(updates["command"])
        if "timeout_seconds" in updates:
            validate_timeout(updates["timeout_seconds"])
        if "working_directory" in updates:
            resolve_working_directory(repo["workspace_path"], updates["working_directory"])
        if "name" in updates:
            updates["name"] = updates["name"].strip()
        updates["updated_at"] = store.now_iso()
        return store.update_command(command_id, updates) or command_item

    def disable_command(self, command_id: str) -> dict:
        if not store.get_command(command_id):
            raise LookupError("Test command not found.")
        return store.update_command(command_id, {"enabled": False, "updated_at": store.now_iso()})

    def run_command(self, command_id: str, request: TestRunRequest) -> dict:
        if request.confirm is not True:
            raise ValueError("Explicit confirmation is required before running tests.")
        command_item = store.get_command(command_id)
        if not command_item:
            raise LookupError("Test command not found.")
        if not command_item["enabled"]:
            raise ValueError("Disabled test commands cannot run.")
        repo = self._repo(command_item["repo_id"])
        validate_command(command_item["command"])
        validate_timeout(command_item["timeout_seconds"])
        cwd = resolve_working_directory(repo["workspace_path"], command_item["working_directory"])
        self._validate_associations(command_item, request)
        now = store.now_iso()
        run = store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "repo_id": repo["id"],
                "project_id": command_item.get("project_id"),
                "task_id": request.task_id,
                "agent_run_id": request.agent_run_id,
                "patch_application_id": request.patch_application_id,
                "test_command_id": command_item["id"],
                "name": command_item["name"],
                "command": command_item["command"],
                "working_directory": command_item["working_directory"],
                "status": "queued",
                "timeout_seconds": command_item["timeout_seconds"],
                "created_at": now,
            }
        )
        started = store.now_iso()
        store.update_run(run["id"], {"status": "running", "started_at": started})
        result = execute(command_item["command"], cwd, command_item["timeout_seconds"])
        return store.update_run(
            run["id"],
            {
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout_text": result.stdout_text,
                "stderr_text": result.stderr_text,
                "combined_output": result.combined_output,
                "duration_ms": result.duration_ms,
                "error": result.error,
                "metadata": result.metadata,
                "completed_at": store.now_iso(),
            },
        )

    def _validate_associations(self, command_item: dict, request: TestRunRequest) -> None:
        project_id = command_item.get("project_id")
        if request.task_id:
            task = task_store.get_task(request.task_id)
            if not task:
                raise LookupError("Attached task not found.")
            if project_id and task.get("project_id") != project_id:
                raise ValueError("Attached task must belong to the test command project.")
        if request.agent_run_id:
            agent_run = agent_store.get_run(request.agent_run_id)
            if not agent_run:
                raise LookupError("Attached agent run not found.")
            if request.task_id and agent_run.get("task_id") != request.task_id:
                raise ValueError("Attached agent run must belong to the attached task.")
        if request.patch_application_id:
            application = patch_store.get_application(request.patch_application_id)
            if not application:
                raise LookupError("Attached patch application not found.")
            if project_id and application.get("project_id") not in {None, project_id}:
                raise ValueError(
                    "Attached patch application must belong to the test command project."
                )


class TestRunnerContextService:
    def context_for_task(self, task_id: str, project_id: str | None = None) -> str:
        runs, _ = store.list_runs(task_id=task_id, limit=5)
        commands: list[dict] = []
        if project_id:
            repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
            for repo in repos:
                commands.extend(store.list_commands(repo["id"], include_disabled=False))
        lines = []
        if commands:
            lines.append("Suggested validation (manual approval required):")
            lines.extend(f"- {item['name']}: {' '.join(item['command'])}" for item in commands[:8])
        if runs:
            lines.append("Stored test results (read-only):")
            lines.extend(_run_summary(item) for item in runs)
        return "\n".join(lines)

    def context_for_prompt(self, prompt: str) -> str:
        if not TEST_INTENT.search(prompt):
            return ""
        runs, _ = store.list_runs(limit=5)
        return "Stored test run context (read-only; never execute from chat):\n" + (
            "\n".join(_run_summary(item) for item in runs) if runs else "No stored test runs."
        )

    def answer_for_prompt(self, prompt: str) -> str | None:
        if not TEST_INTENT.search(prompt):
            return None
        runs, _ = store.list_runs(limit=10)
        if not runs:
            return (
                "There are no stored test runs yet. Tests can only be started from an "
                "explicitly confirmed Test Runner command."
            )
        latest = runs[0]
        excerpt = (
            latest.get("stderr_text")
            or latest.get("stdout_text")
            or latest.get("error")
            or "No output."
        ).strip()[:1200]
        exit_code = latest.get("exit_code")
        exit_label = exit_code if exit_code is not None else "none"
        return (
            f"Latest stored test run: {latest['name']} — {latest['status']}. "
            f"Exit code: {exit_label}.\n\n{excerpt}"
        )


def _run_summary(item: dict) -> str:
    excerpt = (
        (item.get("stderr_text") or item.get("stdout_text") or item.get("error") or "")
        .strip()
        .replace("\n", " ")[:500]
    )
    return f"- {item['name']} [{item['status']}, exit {item.get('exit_code')}]: {excerpt}"
