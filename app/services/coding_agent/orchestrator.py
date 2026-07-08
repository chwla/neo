from __future__ import annotations

import uuid

import app.services.coding_agent.store as store
from app.services.agents import store as agent_store
from app.services.coding_agent.context import CodingContextSelector
from app.services.coding_agent.planner import CodingTaskPlanner
from app.services.coding_agent.safety import clean_objective, require_confirmation, require_pending
from app.services.coding_agent.types import CodingRunCreate
from app.services.files import store as file_store
from app.services.git import store as git_store
from app.services.git.service import GitService
from app.services.git.types import CheckpointCreateRequest
from app.services.patch_apply import store as patch_store
from app.services.patch_apply.service import ControlledPatchApplyService
from app.services.patch_apply.types import PatchApplyRequest
from app.services.patches.service import PatchProposalService
from app.services.patches.types import PatchProposalRequest
from app.services.repos import store as repo_store
from app.services.test_runner import store as test_store
from app.services.test_runner.service import TestRunnerService
from app.services.test_runner.types import TestRunRequest


class CodingAgentOrchestrator:
    def __init__(
        self,
        *,
        task_planner=None,
        context_selector=None,
        patch_service=None,
        patch_apply=None,
        test_runner=None,
        git_service=None,
    ):
        self.task_planner = task_planner or CodingTaskPlanner()
        self.context_selector = context_selector or CodingContextSelector()
        self.patch_service = patch_service or PatchProposalService()
        self.patch_apply = patch_apply or ControlledPatchApplyService()
        self.test_runner = test_runner or TestRunnerService()
        self.git_service = git_service or GitService()

    def start(self, request: CodingRunCreate) -> dict:
        objective = clean_objective(request.objective)
        task, project_id, subtasks = self.task_planner.resolve(
            objective, request.task_id, request.project_id
        )
        repo = self._resolve_repo(request.repo_id, project_id)
        now = store.now_iso()
        agent_run = agent_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "task_id": task.id,
                "project_id": project_id,
                "title": f"Coding agent: {task.title}"[:200],
                "objective": objective,
                "status": "planning",
                "mode": "coding",
                "plan": [],
                "final_output": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": None,
                "cancelled_at": None,
            }
        )
        coding_run = store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "agent_run_id": agent_run["id"],
                "task_id": task.id,
                "project_id": project_id,
                "repo_id": repo["id"],
                "objective": objective,
                "status": "queued",
                "current_iteration": 1,
                "max_iterations": request.max_iterations,
                "selected_files": [],
                "patch_artifact_id": None,
                "patch_application_id": None,
                "test_run_id": None,
                "checkpoint_id": None,
                "error": None,
                "metadata": {
                    "created_subtask_ids": [item.id for item in subtasks],
                    "test_status": "not_run",
                },
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "cancelled_at": None,
            }
        )
        try:
            self._status(coding_run["id"], "planning")
            self._step(
                agent_run["id"],
                "plan",
                "Plan coding workflow",
                f"Objective: {objective}\nMaximum iterations: {request.max_iterations}",
            )
            self._status(coding_run["id"], "selecting_context")
            files = self.context_selector.select(repo, objective, task.id, project_id)
            store.update_run(
                coding_run["id"], {"selected_files": files, "updated_at": store.now_iso()}
            )
            self._step(
                agent_run["id"],
                "select_context",
                "Select bounded code context",
                self._files_text(files),
            )
            self._propose(coding_run["id"], objective)
        except Exception as exc:
            self._fail(coding_run["id"], agent_run["id"], str(exc))
            raise
        return self.detail(coding_run["id"])

    def approve(self, action_id: str, *, confirm: bool, options: dict) -> dict:
        require_confirmation(confirm)
        action = store.get_action(action_id)
        if not action:
            raise LookupError("Coding-agent action request not found.")
        require_pending(action)
        run = self._run(action["coding_run_id"])
        now = store.now_iso()
        store.update_action(action_id, {"status": "approved", "decided_at": now, "updated_at": now})
        store.update_action(action_id, {"status": "executing", "updated_at": store.now_iso()})
        self._decide_waiting_step(run["agent_run_id"], "approved")
        try:
            result = self._execute(action, run, options)
            store.update_action(
                action_id,
                {
                    "status": "completed",
                    "result": result or {},
                    "executed_at": store.now_iso(),
                    "updated_at": store.now_iso(),
                },
            )
        except Exception as exc:
            store.update_action(
                action_id,
                {
                    "status": "failed",
                    "error": str(exc),
                    "executed_at": store.now_iso(),
                    "updated_at": store.now_iso(),
                },
            )
            self._step(
                run["agent_run_id"],
                "waiting_approval",
                f"{action['title']} failed safely",
                str(exc),
                status="failed",
            )
            if action["action_type"] == "apply_patch":
                self._status(run["id"], "waiting_patch_approval")
                self._action(
                    self._run(run["id"]),
                    "revise_patch",
                    "Revise patch after safe rejection",
                    "The patch was not applied. Provide new revision instructions.",
                    {"failed_action_id": action_id},
                )
            elif action["action_type"] == "run_tests":
                self._status(run["id"], "waiting_test_approval")
                self._action(
                    self._run(run["id"]),
                    "run_tests",
                    "Retry a saved test",
                    "No test result was recorded. Select a saved command to retry.",
                    action["payload"],
                )
            elif action["action_type"] == "create_checkpoint":
                self._status(run["id"], "waiting_checkpoint_approval")
                self._action(
                    self._run(run["id"]),
                    "create_checkpoint",
                    "Retry local checkpoint",
                    "The checkpoint was not created. Resolve the reported issue before retrying.",
                    {},
                )
            else:
                self._fail(run["id"], run["agent_run_id"], str(exc))
                raise
        return self.detail(run["id"])

    def reject(self, action_id: str, reason: str | None) -> dict:
        action = store.get_action(action_id)
        if not action:
            raise LookupError("Coding-agent action request not found.")
        require_pending(action)
        now = store.now_iso()
        store.update_action(
            action_id, {"status": "rejected", "error": reason, "decided_at": now, "updated_at": now}
        )
        run = self._run(action["coding_run_id"])
        self._decide_waiting_step(run["agent_run_id"], "rejected")
        if action["action_type"] == "apply_patch":
            self._action(
                run,
                "revise_patch",
                "Revise rejected patch",
                "Provide revision instructions; no patch will be applied.",
                {},
            )
        elif action["action_type"] == "run_tests":
            self._action(
                run,
                "skip_tests",
                "Skip tests",
                "Complete the test gate without executing a command.",
                {},
            )
        elif action["action_type"] == "create_checkpoint":
            self._action(
                run,
                "skip_checkpoint",
                "Skip checkpoint",
                "Complete without creating a Git checkpoint.",
                {},
            )
        return self.detail(run["id"])

    def revise(self, run_id: str, instructions: str) -> dict:
        run = self._run(run_id)
        if run["status"] != "waiting_patch_approval":
            raise ValueError("Patch revision is only available while waiting for patch approval.")
        store.cancel_pending_actions(run_id)
        objective = f"{run['objective']}\n\nRevision instructions: {instructions.strip()}"
        self._step(
            run["agent_run_id"], "patch_proposal", "Revise patch proposal", instructions.strip()
        )
        self._propose(run_id, objective)
        return self.detail(run_id)

    def cancel(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run["status"] in {"completed", "failed", "cancelled"}:
            return self.detail(run_id)
        now = store.now_iso()
        store.cancel_pending_actions(run_id)
        store.update_run(run_id, {"status": "cancelled", "cancelled_at": now, "updated_at": now})
        agent_store.cancel_run(run["agent_run_id"])
        self._step(
            run["agent_run_id"],
            "final",
            "Coding run cancelled",
            "Partial logs and artifacts were preserved.",
            status="cancelled",
        )
        return self.detail(run_id)

    def detail(self, run_id: str) -> dict:
        run = self._run(run_id)
        return {
            "coding_run": run,
            "agent_run": agent_store.get_run(run["agent_run_id"]),
            "steps": agent_store.list_steps(run["agent_run_id"]),
            "action_requests": store.list_actions(run_id),
            "patch_artifact": file_store.get_artifact(run.get("patch_artifact_id"))
            if run.get("patch_artifact_id")
            else None,
            "patch_application": patch_store.get_application(run.get("patch_application_id"))
            if run.get("patch_application_id")
            else None,
            "test_run": test_store.get_run(run.get("test_run_id"))
            if run.get("test_run_id")
            else None,
            "checkpoint": git_store.get_checkpoint(run.get("checkpoint_id"))
            if run.get("checkpoint_id")
            else None,
            "current_action_request": next(
                (
                    item
                    for item in reversed(store.list_actions(run_id))
                    if item["status"] == "pending"
                ),
                None,
            ),
        }

    def _execute(self, action: dict, run: dict, options: dict) -> dict:
        kind = action["action_type"]
        if kind == "apply_patch":
            self._status(run["id"], "applying_patch")
            target_files = action["payload"].get("target_files", [])
            file_id = options.get("file_id")
            if not file_id and len(target_files) == 1:
                file_id = target_files[0].get("file_id")
            if not file_id:
                raise ValueError("Select one proposed target file before applying this patch.")
            if file_id not in {item.get("file_id") for item in target_files}:
                raise ValueError("Selected file is not a target of this patch proposal.")
            application, _ = self.patch_apply.apply(
                action["payload"]["artifact_id"],
                PatchApplyRequest(file_id=file_id, confirm=True),
            )
            store.update_run(
                run["id"],
                {"patch_application_id": application["id"], "updated_at": store.now_iso()},
            )
            self._step(
                run["agent_run_id"],
                "apply_patch",
                "Apply approved patch",
                f"Applied to managed workspace only. Application: {application['id']}",
            )
            commands = test_store.list_commands(run["repo_id"], include_disabled=False)
            self._status(run["id"], "waiting_test_approval")
            if commands:
                self._step(
                    run["agent_run_id"],
                    "select_tests",
                    "Select saved test commands",
                    "Available: " + ", ".join(item["name"] for item in commands),
                )
                self._action(
                    self._run(run["id"]),
                    "run_tests",
                    "Run selected saved test",
                    "Runs only the selected saved command inside the managed workspace.",
                    {
                        "test_commands": [
                            {"id": item["id"], "name": item["name"], "command": item["command"]}
                            for item in commands
                        ]
                    },
                )
            else:
                self._step(
                    run["agent_run_id"],
                    "select_tests",
                    "No saved test commands",
                    "Configure a safe command in Test Runner or explicitly skip tests.",
                )
                self._action(
                    self._run(run["id"]),
                    "skip_tests",
                    "No saved test command",
                    "No test tool will run. Configure a safe command in Test Runner to run tests.",
                    {},
                )
            self._step(
                run["agent_run_id"],
                "waiting_approval",
                "Wait for test approval",
                "No test command can run automatically.",
                status="waiting_approval",
                requires_approval=True,
            )
            return {"patch_application_id": application["id"]}
        if kind == "run_tests":
            command_id = options.get("test_command_id")
            allowed = {item["id"] for item in action["payload"].get("test_commands", [])}
            if command_id not in allowed:
                raise ValueError("Select one of the saved test commands offered by this action.")
            self._status(run["id"], "running_tests")
            test_run = self.test_runner.run_command(
                command_id,
                TestRunRequest(
                    confirm=True,
                    task_id=run["task_id"],
                    agent_run_id=run["agent_run_id"],
                    patch_application_id=run.get("patch_application_id"),
                ),
            )
            store.update_run(
                run["id"], {"test_run_id": test_run["id"], "updated_at": store.now_iso()}
            )
            self._status(run["id"], "analyzing_test_result")
            self._step(
                run["agent_run_id"],
                "run_tests",
                "Run approved tests",
                f"{test_run['name']}: {test_run['status']} (exit {test_run.get('exit_code')})",
            )
            if test_run["status"] == "passed":
                self._checkpoint_gate(self._run(run["id"]), "passed")
            elif run["current_iteration"] < run["max_iterations"]:
                next_iteration = run["current_iteration"] + 1
                store.update_run(
                    run["id"], {"current_iteration": next_iteration, "updated_at": store.now_iso()}
                )
                self._status(run["id"], "proposing_followup_patch")
                failure = (
                    test_run.get("combined_output") or test_run.get("error") or "Tests failed."
                )[-4000:]
                self._step(run["agent_run_id"], "analyze_tests", "Analyze failed tests", failure)
                self._propose(run["id"], f"{run['objective']}\n\nFix this test failure:\n{failure}")
            else:
                self._fail(
                    run["id"],
                    run["agent_run_id"],
                    "Tests failed and the maximum iteration count was reached.",
                )
            return {"test_run_id": test_run["id"], "status": test_run["status"]}
        if kind == "skip_tests":
            metadata = {**run.get("metadata", {}), "test_status": "skipped"}
            store.update_run(run["id"], {"metadata": metadata, "updated_at": store.now_iso()})
            self._step(
                run["agent_run_id"],
                "run_tests",
                "Tests skipped by user",
                "No test command was executed.",
                status="skipped",
            )
            self._checkpoint_gate(self._run(run["id"]), "skipped")
            return {"test_status": "skipped"}
        if kind == "create_checkpoint":
            self._status(run["id"], "creating_checkpoint")
            checkpoint = self.git_service.create_checkpoint(
                run["repo_id"],
                CheckpointCreateRequest(
                    title=f"Coding agent: {run['objective'][:120]}",
                    message="Approved coding-agent checkpoint.",
                    task_id=run["task_id"],
                    agent_run_id=run["agent_run_id"],
                    patch_application_id=run.get("patch_application_id"),
                    test_run_id=run.get("test_run_id"),
                    confirm=True,
                ),
            )
            store.update_run(
                run["id"], {"checkpoint_id": checkpoint["id"], "updated_at": store.now_iso()}
            )
            self._step(
                run["agent_run_id"],
                "checkpoint",
                "Create approved checkpoint",
                f"Checkpoint: {checkpoint['commit_sha']}",
            )
            self._complete(self._run(run["id"]), checkpoint_created=True)
            return {"checkpoint_id": checkpoint["id"]}
        if kind == "skip_checkpoint":
            self._step(
                run["agent_run_id"],
                "checkpoint",
                "Checkpoint skipped by user",
                "No commit was created.",
                status="skipped",
            )
            self._complete(run, checkpoint_created=False)
            return {"checkpoint_status": "skipped"}
        if kind == "revise_patch":
            return {"next": "Submit revision instructions through the revise-patch endpoint."}
        raise ValueError(f"Unsupported coding-agent action: {kind}")

    def _propose(self, run_id: str, objective: str) -> None:
        run = self._run(run_id)
        self._status(run_id, "proposing_patch")
        artifact = self.patch_service.propose(
            PatchProposalRequest(
                objective=objective,
                task_id=run["task_id"],
                project_id=run.get("project_id"),
                agent_run_id=run["agent_run_id"],
                file_ids=[item["file_id"] for item in run["selected_files"]],
            )
        )
        store.update_run(
            run_id, {"patch_artifact_id": artifact["id"], "updated_at": store.now_iso()}
        )
        self._step(
            run["agent_run_id"],
            "patch_proposal",
            "Create patch proposal",
            f"Artifact {artifact['id']} ({artifact['artifact_type']}). "
            "This patch has not been applied.",
        )
        if artifact["artifact_type"] != "patch_proposal":
            raise ValueError(
                "A reliable unified diff could not be generated; narrow the scope "
                "and revise the run."
            )
        self._status(run_id, "waiting_patch_approval")
        self._step(
            run["agent_run_id"],
            "waiting_approval",
            "Wait for patch approval",
            "No patch can be applied automatically.",
            status="waiting_approval",
            requires_approval=True,
        )
        self._action(
            self._run(run_id),
            "apply_patch",
            "Approve and apply patch",
            "Applies only to Neo's managed workspace copy; the original repository is untouched.",
            {
                "artifact_id": artifact["id"],
                "target_files": artifact.get("metadata", {}).get("target_files", []),
            },
        )

    def _checkpoint_gate(self, run: dict, test_status: str) -> None:
        metadata = {**run.get("metadata", {}), "test_status": test_status}
        store.update_run(
            run["id"],
            {
                "status": "waiting_checkpoint_approval",
                "metadata": metadata,
                "updated_at": store.now_iso(),
            },
        )
        self._step(
            run["agent_run_id"],
            "analyze_tests",
            "Analyze validation result",
            f"Validation status: {test_status}.",
        )
        self._action(
            self._run(run["id"]),
            "create_checkpoint",
            "Create local checkpoint",
            "Creates a local managed-workspace checkpoint only; no remote is contacted.",
            {},
        )
        self._step(
            run["agent_run_id"],
            "waiting_approval",
            "Wait for checkpoint approval",
            "No checkpoint can be created automatically.",
            status="waiting_approval",
            requires_approval=True,
        )

    def _complete(self, run: dict, *, checkpoint_created: bool) -> None:
        detail = self.detail(run["id"])
        test = detail.get("test_run")
        patch = detail.get("patch_application")
        files = ", ".join(item["relative_path"] for item in run["selected_files"])
        test_status = (
            test.get("status") if test else run.get("metadata", {}).get("test_status", "not run")
        )
        summary = (
            f"Patch applied: {'yes' if patch and patch.get('status') == 'applied' else 'no'}\n"
            f"Tests run: {'yes' if test else 'no'}\n"
            f"Test status: {test_status}\n"
            f"Checkpoint created: {'yes' if checkpoint_created else 'no'}\n"
            f"Files changed: {files or 'none recorded'}\n"
            "Remaining risks: review the managed diff and validation output.\n"
            "Next recommended action: inspect the final workspace state."
        )
        now = store.now_iso()
        store.update_run(run["id"], {"status": "completed", "completed_at": now, "updated_at": now})
        agent_store.update_run(
            run["agent_run_id"],
            {
                "status": "completed",
                "final_output": summary,
                "completed_at": now,
                "updated_at": now,
            },
        )
        self._step(run["agent_run_id"], "final", "Final coding summary", summary)

    def _resolve_repo(self, repo_id: str | None, project_id: str | None) -> dict:
        if repo_id:
            repo = repo_store.get_repo(repo_id)
            if not repo:
                raise LookupError("Managed repository not found.")
            if project_id and repo.get("project_id") != project_id:
                raise ValueError("Selected repository does not belong to the selected project.")
            return repo
        if not project_id:
            raise ValueError("Select a managed repository for the coding run.")
        repos, _ = repo_store.list_repos(project_id=project_id, limit=20)
        if len(repos) != 1:
            raise ValueError(
                "Select a repository explicitly when the project does not have exactly one repo."
            )
        return repos[0]

    def _run(self, run_id: str) -> dict:
        run = store.get_run(run_id)
        if not run:
            raise LookupError("Coding-agent run not found.")
        return run

    def _status(self, run_id: str, status: str) -> None:
        store.update_run(run_id, {"status": status, "updated_at": store.now_iso()})
        run = self._run(run_id)
        if status in {
            "planning",
            "selecting_context",
            "proposing_patch",
            "applying_patch",
            "running_tests",
            "analyzing_test_result",
            "proposing_followup_patch",
            "creating_checkpoint",
        }:
            agent_store.update_run(
                run["agent_run_id"], {"status": "running", "updated_at": store.now_iso()}
            )
        elif status.startswith("waiting_"):
            agent_store.update_run(
                run["agent_run_id"], {"status": "waiting_approval", "updated_at": store.now_iso()}
            )

    def _action(
        self, run: dict, action_type: str, title: str, description: str, payload: dict
    ) -> dict:
        now = store.now_iso()
        return store.insert_action(
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

    def _step(
        self,
        run_id: str,
        step_type: str,
        title: str,
        output: str,
        *,
        status="completed",
        requires_approval=False,
    ) -> None:
        existing = agent_store.list_steps(run_id)
        now = store.now_iso()
        agent_store.insert_step(
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "step_index": len(existing),
                "step_type": step_type,
                "title": title,
                "status": status,
                "input": {},
                "output_text": output,
                "error": None,
                "requires_approval": requires_approval,
                "approval_status": None,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": None if status == "waiting_approval" else now,
            }
        )

    @staticmethod
    def _decide_waiting_step(agent_run_id: str, decision: str) -> None:
        waiting = next(
            (
                step
                for step in reversed(agent_store.list_steps(agent_run_id))
                if step["status"] == "waiting_approval"
            ),
            None,
        )
        if waiting:
            agent_store.update_step(
                waiting["id"],
                {
                    "status": "completed" if decision == "approved" else "skipped",
                    "approval_status": decision,
                    "completed_at": store.now_iso(),
                    "updated_at": store.now_iso(),
                },
            )

    def _fail(self, run_id: str, agent_run_id: str, error: str) -> None:
        now = store.now_iso()
        store.update_run(
            run_id, {"status": "failed", "error": error, "completed_at": now, "updated_at": now}
        )
        agent_store.update_run(
            agent_run_id,
            {"status": "failed", "error": error, "completed_at": now, "updated_at": now},
        )
        self._step(agent_run_id, "final", "Coding run failed", error, status="failed")

    @staticmethod
    def _files_text(files: list[dict]) -> str:
        return "Files considered:\n" + "\n".join(
            f"- {item['relative_path']} — {item['reason']} Source: {item['source']}."
            for item in files
        )
