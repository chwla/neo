from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.services.agents import store as agent_store
from app.services.chat_intent import is_internal_chat_command
from app.services.code_index import store as index_store
from app.services.files import store as file_store
from app.services.files.extractors import extract_text
from app.services.git import store
from app.services.git.executor import GitResult, git_available, run_git
from app.services.git.parser import parse_name_only, parse_status
from app.services.git.safety import (
    validate_message,
    validate_relative_path,
    validate_sha,
    validate_workspace,
)
from app.services.git.types import CheckpointCreateRequest, CheckpointRestoreRequest, GitInitRequest
from app.services.patch_apply import store as patch_store
from app.services.repos import store as repo_store
from app.services.tasks import store as task_store
from app.services.test_runner import store as test_store

GIT_INTENT = re.compile(
    r"\b(git status|repo diff|show (?:the )?diff|what changed|checkpoints?|roll back|rollback)\b",
    re.I,
)
EXCLUDE_PATTERNS = """# Neo generated-file exclusions
__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
node_modules/
dist/
build/
coverage/
.coverage
"""


class GitService:
    def _repo(self, repo_id: str) -> dict:
        repo = repo_store.get_repo(repo_id)
        if not repo:
            raise LookupError("Managed repository not found or has been deleted.")
        validate_workspace(repo)
        return repo

    def status(self, repo_id: str) -> dict:
        repo = self._repo(repo_id)
        state = store.get_git_repo(repo_id)
        if not git_available():
            return {
                "initialized": bool(state and state["git_initialized"]),
                "available": False,
                "head": state.get("current_head") if state else None,
                "default_branch": state.get("default_branch") if state else None,
                "changed_files": [],
                "clean": True,
                "error": "Git is not installed in this runtime.",
            }
        if not state or not state["git_initialized"]:
            return {
                "initialized": False,
                "available": True,
                "head": None,
                "default_branch": None,
                "changed_files": [],
                "clean": True,
                "error": None,
            }
        root = self._initialized_root(repo, state)
        result = self._execute(repo_id, root, "status", ["status", "--porcelain=v1"])
        changed = parse_status(result.stdout)
        head = self._head(root)
        branch = self._branch(root)
        store.update_git_repo(
            repo_id,
            {
                "current_head": head,
                "default_branch": branch,
                "metadata": {**state.get("metadata", {}), "last_status": changed},
                "updated_at": store.now_iso(),
            },
        )
        return {
            "initialized": True,
            "available": True,
            "head": head,
            "default_branch": branch,
            "changed_files": changed,
            "clean": not changed,
            "error": None,
        }

    def initialize(self, repo_id: str, request: GitInitRequest) -> tuple[dict, dict]:
        if request.confirm is not True:
            raise ValueError("Git initialization requires confirm=true.")
        repo = self._repo(repo_id)
        if not git_available():
            raise RuntimeError("Git is not installed in this runtime.")
        existing = store.get_git_repo(repo_id)
        if existing and existing["git_initialized"]:
            checkpoints, _ = store.list_checkpoints(repo_id=repo_id, limit=1)
            return existing, checkpoints[0] if checkpoints else None
        root = validate_workspace(repo)
        created_at = store.now_iso()
        outputs: list[str] = []
        try:
            outputs.append(run_git(root, ["init"]).stdout)
            self._write_excludes(root)
            outputs.append(run_git(root, ["config", "--local", "user.name", "Neo"]).stdout)
            outputs.append(run_git(root, ["config", "--local", "user.email", "neo@local"]).stdout)
            before = parse_status(run_git(root, ["status", "--porcelain=v1"]).stdout)
            if not before:
                raise ValueError("Managed repository has no files to checkpoint.")
            run_git(root, ["add", "--", "."])
            commit = run_git(root, ["commit", "-m", "Neo initial workspace checkpoint"])
            outputs.extend([commit.stdout, commit.stderr])
            head = self._head(root)
            branch = self._branch(root)
            changed_files, stats = self._checkpoint_details(root, head)
            checkpoint = store.insert_checkpoint(
                {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "project_id": repo.get("project_id"),
                    "commit_sha": head,
                    "title": "Initial workspace checkpoint",
                    "message": "Initial Neo-managed repository snapshot.",
                    "changed_files": changed_files,
                    "stats": stats,
                    "status": "created",
                    "created_at": created_at,
                }
            )
            state = store.insert_git_repo(
                {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "project_id": repo.get("project_id"),
                    "status": "ready",
                    "git_initialized": True,
                    "current_head": head,
                    "default_branch": branch,
                    "metadata": {"last_status": []},
                    "created_at": created_at,
                    "updated_at": created_at,
                    "initialized_at": created_at,
                }
            )
            self._record_operation(
                repo_id,
                "init",
                "completed",
                stdout_text="\n".join(outputs),
                checkpoint_id=checkpoint["id"],
                metadata={"head": head, "branch": branch},
            )
            return state, checkpoint
        except Exception as exc:
            self._record_operation(repo_id, "init", "failed", error=str(exc))
            raise

    def diff(self, repo_id: str, path: str | None = None) -> dict:
        repo = self._repo(repo_id)
        state = self._require_initialized(repo)
        root = self._initialized_root(repo, state)
        safe_path = validate_relative_path(root, path) if path else None
        args = ["diff", "--", safe_path] if safe_path else ["diff", "--"]
        result = self._execute(repo_id, root, "diff", args, metadata={"path": safe_path})
        metadata = {
            **state.get("metadata", {}),
            "last_diff": result.stdout,
            "last_diff_path": safe_path,
        }
        store.update_git_repo(repo_id, {"metadata": metadata, "updated_at": store.now_iso()})
        return {
            "repo_id": repo_id,
            "path": safe_path,
            "diff": result.stdout,
            "truncated": result.truncated,
        }

    def create_checkpoint(self, repo_id: str, request: CheckpointCreateRequest) -> dict:
        if request.confirm is not True:
            raise ValueError("Checkpoint creation requires confirm=true.")
        repo = self._repo(repo_id)
        state = self._require_initialized(repo)
        root = self._initialized_root(repo, state)
        title = validate_message(request.title)
        message = validate_message(request.message) if request.message else title
        self._validate_associations(repo, request)
        changed = parse_status(run_git(root, ["status", "--porcelain=v1"]).stdout)
        if not changed:
            raise ValueError("Managed repository is clean; there is nothing to checkpoint.")
        untracked = [item["path"] for item in changed if item["status"] == "untracked"]
        registered, _ = repo_store.list_repo_files(repo["id"], limit=10000)
        registered_paths = {item["relative_path"] for item in registered}
        unknown_untracked = [path for path in untracked if path not in registered_paths]
        if unknown_untracked:
            raise ValueError(
                "Checkpoint refused because untracked files exist that are not registered. "
                "Neo checkpoints only files registered in the managed repository workspace."
            )
        synced = self._sync_metadata(repo)
        index_store.mark_stale(
            repo["id"], "Git checkpoint captured workspace changes", store.now_iso()
        )
        operation_id = str(uuid.uuid4())
        created_at = store.now_iso()
        try:
            run_git(root, ["add", "--", "."])
            commit = run_git(root, ["commit", "-m", message])
            head = self._head(root)
            changed_files, stats = self._checkpoint_details(root, head)
            stats["synced_files"] = synced
            checkpoint = store.insert_checkpoint(
                {
                    "id": str(uuid.uuid4()),
                    "repo_id": repo_id,
                    "project_id": repo.get("project_id"),
                    "task_id": request.task_id,
                    "agent_run_id": request.agent_run_id,
                    "patch_application_id": request.patch_application_id,
                    "test_run_id": request.test_run_id,
                    "commit_sha": head,
                    "title": title,
                    "message": request.message,
                    "changed_files": changed_files,
                    "stats": stats,
                    "status": "created",
                    "created_at": created_at,
                }
            )
            store.update_git_repo(
                repo_id,
                {
                    "current_head": head,
                    "metadata": {**state.get("metadata", {}), "last_status": []},
                    "updated_at": store.now_iso(),
                },
            )
            store.insert_operation(
                {
                    "id": operation_id,
                    "repo_id": repo_id,
                    "checkpoint_id": checkpoint["id"],
                    "operation_type": "commit",
                    "status": "completed",
                    "stdout_text": commit.stdout,
                    "stderr_text": commit.stderr,
                    "metadata": {"head": head},
                    "created_at": created_at,
                    "completed_at": store.now_iso(),
                }
            )
            return checkpoint
        except Exception as exc:
            store.insert_operation(
                {
                    "id": operation_id,
                    "repo_id": repo_id,
                    "operation_type": "commit",
                    "status": "failed",
                    "error": str(exc),
                    "created_at": created_at,
                    "completed_at": store.now_iso(),
                }
            )
            raise

    def read_checkpoint(self, checkpoint_id: str) -> tuple[dict, list[dict]]:
        checkpoint = store.get_checkpoint(checkpoint_id)
        if not checkpoint:
            raise LookupError("Git checkpoint not found.")
        operations, _ = store.list_operations(
            checkpoint["repo_id"], checkpoint_id=checkpoint_id, limit=100
        )
        return checkpoint, operations

    def restore(self, checkpoint_id: str, request: CheckpointRestoreRequest) -> dict:
        if request.confirm is not True:
            raise ValueError("Checkpoint restore requires confirm=true.")
        checkpoint = store.get_checkpoint(checkpoint_id)
        if not checkpoint:
            raise LookupError("Git checkpoint not found.")
        repo = self._repo(checkpoint["repo_id"])
        state = self._require_initialized(repo)
        root = self._initialized_root(repo, state)
        sha = validate_sha(checkpoint["commit_sha"])
        current = parse_status(run_git(root, ["status", "--porcelain=v1"]).stdout)
        untracked = [item["path"] for item in current if item["status"] == "untracked"]
        if untracked:
            raise ValueError(
                "Restore refused because untracked files exist. Create a checkpoint or remove "
                "them through a controlled workspace action first."
            )
        try:
            result = run_git(root, ["restore", f"--source={sha}", "--worktree", "--", "."])
            synced = self._sync_metadata(repo)
            now = store.now_iso()
            index_store.mark_stale(repo["id"], "Git checkpoint restore", now)
            store.update_git_repo(
                repo["id"],
                {
                    "current_head": self._head(root),
                    "metadata": {
                        **state.get("metadata", {}),
                        "last_status": parse_status(
                            run_git(root, ["status", "--porcelain=v1"]).stdout
                        ),
                        "last_restored_checkpoint_id": checkpoint_id,
                    },
                    "updated_at": now,
                },
            )
            updated = store.update_checkpoint(
                checkpoint_id,
                {"status": "restored", "stats": {**checkpoint["stats"], "synced_files": synced}},
            )
            self._record_operation(
                repo["id"],
                "restore",
                "completed",
                stdout_text=result.stdout,
                stderr_text=result.stderr,
                checkpoint_id=checkpoint_id,
                metadata={"commit_sha": sha, "synced_files": synced},
            )
            return updated
        except Exception as exc:
            self._record_operation(
                repo["id"],
                "restore",
                "failed",
                error=str(exc),
                checkpoint_id=checkpoint_id,
                metadata={"commit_sha": sha},
            )
            raise

    def _require_initialized(self, repo: dict) -> dict:
        state = store.get_git_repo(repo["id"])
        if not state or not state["git_initialized"]:
            raise ValueError("Initialize Git checkpointing for this managed repository first.")
        return state

    @staticmethod
    def _initialized_root(repo: dict, state: dict) -> Path:
        root = validate_workspace(repo)
        git_dir = root / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir() or not state["git_initialized"]:
            raise ValueError("Managed Git repository state is unavailable or unsafe.")
        return root

    @staticmethod
    def _write_excludes(root: Path) -> None:
        target = root / ".git" / "info" / "exclude"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(EXCLUDE_PATTERNS, encoding="utf-8")

    @staticmethod
    def _head(root: Path) -> str:
        return validate_sha(run_git(root, ["rev-parse", "HEAD"]).stdout.strip())

    @staticmethod
    def _branch(root: Path) -> str:
        return run_git(root, ["branch", "--show-current"]).stdout.strip() or "detached"

    @staticmethod
    def _checkpoint_details(root: Path, sha: str) -> tuple[list[dict], dict]:
        names = parse_name_only(run_git(root, ["show", "--name-only", "--format=", sha]).stdout)
        stat = run_git(root, ["show", "--stat", "--summary", sha])
        return (
            [{"path": path, "status": "committed"} for path in names],
            {
                "summary": stat.stdout,
                "truncated": stat.truncated,
            },
        )

    def _execute(
        self,
        repo_id: str,
        root: Path,
        operation_type: str,
        args: list[str],
        *,
        metadata: dict | None = None,
    ) -> GitResult:
        try:
            result = run_git(root, args)
            self._record_operation(
                repo_id,
                operation_type,
                "completed",
                stdout_text=result.stdout,
                stderr_text=result.stderr,
                metadata={**(metadata or {}), "truncated": result.truncated},
            )
            return result
        except Exception as exc:
            self._record_operation(
                repo_id, operation_type, "failed", error=str(exc), metadata=metadata or {}
            )
            raise

    @staticmethod
    def _record_operation(
        repo_id: str,
        operation_type: str,
        status: str,
        *,
        stdout_text: str = "",
        stderr_text: str = "",
        error: str | None = None,
        checkpoint_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        now = store.now_iso()
        return store.insert_operation(
            {
                "id": str(uuid.uuid4()),
                "repo_id": repo_id,
                "checkpoint_id": checkpoint_id,
                "operation_type": operation_type,
                "status": status,
                "stdout_text": stdout_text,
                "stderr_text": stderr_text,
                "error": error,
                "metadata": metadata or {},
                "created_at": now,
                "completed_at": now,
            }
        )

    @staticmethod
    def _validate_associations(repo: dict, request: CheckpointCreateRequest) -> None:
        if request.task_id:
            task = task_store.get_task(request.task_id)
            if not task:
                raise LookupError("Attached task not found.")
            if repo.get("project_id") and task.get("project_id") != repo["project_id"]:
                raise ValueError("Attached task must belong to the repository project.")
        if request.agent_run_id:
            run = agent_store.get_run(request.agent_run_id)
            if not run:
                raise LookupError("Attached agent run not found.")
            if request.task_id and run.get("task_id") != request.task_id:
                raise ValueError("Attached agent run must belong to the attached task.")
        if request.patch_application_id:
            application = patch_store.get_application(request.patch_application_id)
            if not application:
                raise LookupError("Attached patch application not found.")
            mapping = repo_store.get_repo_file_by_file_id(application["file_id"])
            if not mapping or mapping["repo_id"] != repo["id"]:
                raise ValueError("Attached patch application belongs to another repository.")
        if request.test_run_id:
            test_run = test_store.get_run(request.test_run_id)
            if not test_run:
                raise LookupError("Attached test run not found.")
            if test_run["repo_id"] != repo["id"]:
                raise ValueError("Attached test run belongs to another repository.")

    @staticmethod
    def _sync_metadata(repo: dict) -> int:
        root = validate_workspace(repo)
        mappings, _ = repo_store.list_repo_files(repo["id"], limit=500)
        max_chars = get_settings().workspace_extracted_text_max_chars
        synced = 0
        for mapping in mappings:
            file_item = file_store.get_file(mapping["file_id"], include_deleted=True)
            if not file_item:
                continue
            path = root / mapping["relative_path"]
            if path.is_symlink() or (path.exists() and root not in path.resolve().parents):
                raise ValueError("Restored repository file escaped the managed workspace.")
            if not path.is_file():
                file_store.update_file(
                    file_item["id"],
                    {"deleted": True, "sha256": None, "size_bytes": 0, "extracted_text": None},
                )
                repo_store.update_repo_file_hash(file_item["id"], None, 0)
                synced += 1
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            extracted, extraction = extract_text(mapping["relative_path"], content, max_chars)
            updated = file_store.update_file(
                file_item["id"],
                {
                    "deleted": False,
                    "sha256": digest,
                    "size_bytes": len(content),
                    "extracted_text": extracted,
                    "summary": None,
                    "metadata_json": {**file_item.get("metadata", {}), **extraction},
                },
            )
            if not updated:
                raise RuntimeError("Workspace file metadata synchronization failed.")
            repo_store.update_repo_file_hash(file_item["id"], digest, len(content))
            synced += 1
        return synced


class GitContextService:
    def context_for_task(self, task_id: str, project_id: str | None = None) -> str:
        checkpoints, _ = store.list_checkpoints(task_id=task_id, limit=5)
        if not checkpoints and project_id:
            repos, _ = repo_store.list_repos(project_id=project_id, limit=10)
            for repo in repos:
                repo_checkpoints, _ = store.list_checkpoints(repo_id=repo["id"], limit=3)
                checkpoints.extend(repo_checkpoints)
        return _context_text(checkpoints)

    def context_for_prompt(self, prompt: str) -> str:
        if not GIT_INTENT.search(prompt):
            return ""
        checkpoints, _ = store.list_checkpoints(limit=5)
        operations = []
        for checkpoint in checkpoints[:2]:
            rows, _ = store.list_operations(checkpoint["repo_id"], limit=5)
            operations.extend(rows)
        diff = next(
            (item["stdout_text"] for item in operations if item["operation_type"] == "diff"), ""
        )
        text = _context_text(checkpoints) or "No stored checkpoints."
        if diff:
            text += f"\nLast stored diff (read-only):\n{diff[:4000]}"
        return "Controlled Git context (read-only; never commit or restore from chat):\n" + text

    def answer_for_prompt(self, prompt: str) -> str | None:
        if not is_internal_chat_command(prompt, "git"):
            return None
        checkpoints, _ = store.list_checkpoints(limit=5)
        if not checkpoints:
            return (
                "There are no stored Git checkpoints yet. Open the repository Git / "
                "Checkpoints panel to initialize or create one explicitly."
            )
        latest = checkpoints[0]
        files = ", ".join(item.get("path", "") for item in latest["changed_files"][:10])
        return (
            f"Latest checkpoint: {latest['title']} ({latest['commit_sha'][:12]}), "
            f"status {latest['status']}. Files: {files or 'none recorded'}. "
            "Commit and restore actions require explicit confirmation in the Git panel."
        )


def _context_text(checkpoints: list[dict]) -> str:
    if not checkpoints:
        return ""
    lines = ["Stored Git checkpoints:"]
    for item in checkpoints[:5]:
        files = ", ".join(entry.get("path", "") for entry in item["changed_files"][:8])
        lines.append(
            f"- {item['title']} [{item['commit_sha'][:12]}, {item['status']}]: "
            f"{files or 'no files recorded'}"
        )
    return "\n".join(lines)
