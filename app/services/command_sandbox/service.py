from __future__ import annotations

from pathlib import Path, PurePosixPath

from app.services.command_sandbox import policy, store
from app.services.command_sandbox.redaction import redact_output
from app.services.command_sandbox.runner import run
from app.services.command_sandbox.types import CommandRequest
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

    def approve(self, run_id: str, confirm: bool) -> dict:
        item = self.require(run_id)
        if not confirm:
            raise ValueError("Explicit confirmation is required.")
        if item["status"] != "proposed" or not item["policy_decision"]["allowed"]:
            raise ValueError("Only an allowed proposed command can be approved.")
        return store.update(run_id, {"approved": 1, "status": "approved"})

    def execute(self, run_id: str) -> dict:
        item = self.require(run_id)
        if item["status"] != "approved" or not item["approved"]:
            raise ValueError("Command execution requires explicit approval.")
        workspace = self.workspace_root(item["workspace_id"])
        cwd = self.resolve_cwd(workspace, item["cwd"])
        item = store.update(run_id, {"status": "running", "started_at": store.now_iso()})
        result = run(item["command"], cwd, item["timeout_ms"])
        stdout, stdout_redaction = redact_output(result["stdout_text"])
        stderr, stderr_redaction = redact_output(result["stderr_text"])
        return store.update(
            run_id,
            {
                **result,
                "stdout_text": stdout,
                "stderr_text": stderr,
                "redaction_summary": {"stdout": stdout_redaction, "stderr": stderr_redaction},
                "status": "timed_out" if result["status"] == "timed_out" else "completed",
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
    def resolve_cwd(root: Path, cwd: str) -> Path:
        path = PurePosixPath(cwd.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("cwd must be a managed-workspace relative path.")
        candidate = (root / Path(*path.parts)).resolve(strict=True)
        if candidate != root and root not in candidate.parents:
            raise ValueError("cwd escapes the managed workspace.")
        if not candidate.is_dir():
            raise ValueError("cwd is not an existing managed workspace directory.")
        return candidate
