from __future__ import annotations

import os
from pathlib import Path

from app.core.paths import is_within, normalize_relative_parts
from app.services.command_sandbox import policy, store
from app.services.command_sandbox.redaction import redact_output
from app.services.command_sandbox.runner import run
from app.services.command_sandbox.types import CommandRequest
from app.services.isolation.launch import HumanApproval
from app.services.repos import store as repo_store


class CommandSandboxService:
    def validate(self, request: CommandRequest) -> dict:
        return policy.validate(request.command, request.category, request.cwd)

    def propose(self, request: CommandRequest) -> dict:
        decision = self.validate(request)
        return store.create(
            {
                "workspace_id": request.workspace_id,
                "command": request.command,
                "cwd": request.cwd,
                "category": request.category,
                "status": "proposed" if decision["allowed"] else "blocked",
                "timeout_ms": request.timeout_ms or policy.TIMEOUTS_MS[request.category],
                "policy_decision": decision,
                "created_by": request.created_by,
            }
        )

    def approve(
        self,
        run_id: str,
        confirm: bool,
        *,
        approver: str = "user",
        acknowledge_unsandboxed: bool = False,
    ) -> dict:
        """Approve one proposed run.

        ``acknowledge_unsandboxed`` records that the approver was shown that the run
        would have no sandbox. It is what ``isolation.launch`` requires before it
        will start the process unsandboxed; without it such a run is refused.
        """

        item = self.require(run_id)
        if not confirm:
            raise ValueError("Explicit confirmation is required.")
        if item["status"] != "proposed" or not item["policy_decision"]["allowed"]:
            raise ValueError("Only an allowed proposed command can be approved.")
        return store.update(
            run_id,
            {
                "approved": 1,
                "status": "approved",
                "approved_by": approver,
                "unsandboxed_ack": 1 if acknowledge_unsandboxed else 0,
            },
        )

    def execute(self, run_id: str) -> dict:
        item = self.require(run_id)
        if item["status"] != "approved" or not item["approved"]:
            raise ValueError("Command execution requires explicit approval.")
        workspace = self.workspace_root(item["workspace_id"])
        # Resolved before the claim so an invalid cwd fails without stranding the run
        # in "running".
        cwd = self.resolve_cwd(workspace, item["cwd"])
        if item["category"] == "read_only":
            self.ensure_arguments_inside(workspace, cwd, item["command"])
        item = store.claim_for_execution(run_id, store.now_iso())
        if item is None:
            raise ValueError("Command execution requires explicit approval.")
        approval = (
            HumanApproval(
                subject=run_id,
                approver=item.get("approved_by") or "",
                acknowledged_exposure=True,
            )
            if item.get("unsandboxed_ack")
            else None
        )
        result = run(
            item["command"],
            cwd,
            item["timeout_ms"],
            workspace=workspace,
            subject=run_id,
            approval=approval,
        )
        stdout, stdout_redaction = redact_output(result["stdout_text"])
        stderr, stderr_redaction = redact_output(result["stderr_text"])
        return store.update(
            run_id,
            {
                **result,
                "stdout_text": stdout,
                "stderr_text": stderr,
                "redaction_summary": {"stdout": stdout_redaction, "stderr": stderr_redaction},
                # A refused or failed launch stays visible as such; reporting it as
                # "completed" told the agent and the panel that something had run.
                "status": result["status"]
                if result["status"] in {"timed_out", "failed"}
                else "completed",
                "completed_at": store.now_iso(),
            },
        )

    def cancel(self, run_id: str) -> dict:
        item = self.require(run_id)
        if item["status"] in {"completed", "timed_out"}:
            raise ValueError("Completed command runs cannot be cancelled.")
        return store.update(run_id, {"status": "cancelled", "completed_at": store.now_iso()})

    def require(self, run_id: str) -> dict:
        item = store.get(run_id)
        if not item:
            raise LookupError("Command run not found.")
        return item

    def workspace_root(self, workspace_id: str) -> Path:
        repo = repo_store.get_repo(workspace_id)
        if not repo:
            raise LookupError("Managed workspace not found.")
        root = Path(repo["workspace_path"]).resolve(strict=True)
        return root

    @staticmethod
    def ensure_arguments_inside(root: Path, cwd: Path, command: list[str]) -> None:
        """Refuse an argument naming something that resolves outside the workspace.

        The policy rejects absolute and ``..`` paths as typed, but a symlink inside
        the workspace can point anywhere, and ``cat``, ``head`` and the rest follow
        a link they are handed. On macOS the isolation layer would stop the read;
        elsewhere nothing else does. Arguments that name nothing on disk (patterns,
        revisions) are not paths and pass.
        """

        for arg in command[1:]:
            value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
            if not value or value.startswith("-"):
                continue
            candidate = cwd / value
            if os.path.lexists(candidate) and not is_within(root, candidate.resolve()):
                raise ValueError(f"'{value}' leads outside the workspace, so it cannot be read.")

    @staticmethod
    def resolve_cwd(root: Path, cwd: str) -> Path:
        """Resolve a workspace-relative working directory.

        Shares ``normalize_relative_parts`` with the file tools rather than
        restating the rules, so a Windows drive path is rejected here for the
        same reason and with the same wording it is rejected there.
        """

        if cwd.strip() in {"", ".", "/"}:
            return root
        try:
            parts = normalize_relative_parts(cwd)
        except ValueError as exc:
            raise ValueError(f"cwd must be a managed-workspace relative path. {exc}") from exc
        candidate = root.joinpath(*parts).resolve(strict=True)
        if not is_within(root, candidate):
            raise ValueError("cwd escapes the managed workspace.")
        if not candidate.is_dir():
            raise ValueError("cwd is not an existing managed workspace directory.")
        return candidate
