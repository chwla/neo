import ast
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents import store as agent_store
from app.services.agents.runner import AgentRunner
from app.services.code_index import store as index_store
from app.services.files import store as file_store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate
from app.services.git.safety import validate_git_args
from app.services.git.service import GitContextService
from app.services.patch_apply import store as patch_store
from app.services.projects import ProjectCreate, ProjectsService
from app.services.repos import store as repo_store
from app.services.tasks import TaskCreate, TasksService
from app.services.test_runner import store as test_store


class ControlledGitIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "workspace-files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "workspace-repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(ProjectCreate(title="Git project"))
        self.task = TasksService().create_task(
            TaskCreate(title="Checkpoint change", project_id=self.project.id)
        )
        self.source = self.root / "original"
        self.source.mkdir()
        self.original_content = b"VALUE = 1\n"
        (self.source / "app.py").write_bytes(self.original_content)
        response = self.client.post(
            "/api/repos/register",
            json={"path": str(self.source), "project_id": self.project.id, "confirm": True},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.repo = response.json()["repo"]
        self.repo_record = repo_store.get_repo(self.repo["id"])
        self.managed = Path(self.repo_record["workspace_path"])
        self.mapping = repo_store.list_repo_files(self.repo["id"])[0][0]

    def tearDown(self):
        get_settings.cache_clear()
        for name in ("NEO_DATABASE_URL", "NEO_WORKSPACE_FILES_DIR", "NEO_WORKSPACE_REPOS_DIR"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def initialize(self):
        response = self.client.post(
            f"/api/git/repos/{self.repo['id']}/init", json={"confirm": True}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def seed_associations(self):
        now = agent_store.now_iso()
        agent = agent_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "task_id": self.task.id,
                "project_id": self.project.id,
                "title": "Git context",
                "objective": "Review the checkpoint",
                "status": "completed",
                "mode": "assist",
                "plan": [],
                "final_output": "done",
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": now,
                "cancelled_at": None,
            }
        )
        artifact = WorkspaceFilesService().create_artifact(
            ArtifactCreate(
                title="Git patch",
                artifact_type="patch_proposal",
                content="diff",
                project_id=self.project.id,
                task_id=self.task.id,
                agent_run_id=agent["id"],
            )
        )
        application = patch_store.insert_application(
            {
                "id": str(uuid.uuid4()),
                "artifact_id": artifact["id"],
                "file_id": self.mapping["file_id"],
                "task_id": self.task.id,
                "project_id": self.project.id,
                "agent_run_id": agent["id"],
                "status": "applied",
                "original_sha256": "old",
                "new_sha256": "new",
                "original_content": "before",
                "new_content": "after",
                "patch_text": "diff",
                "created_at": now,
                "applied_at": now,
            }
        )
        command = test_store.insert_command(
            {
                "id": str(uuid.uuid4()),
                "repo_id": self.repo["id"],
                "project_id": self.project.id,
                "name": "Python tests",
                "command": ["python", "-m", "unittest"],
                "working_directory": ".",
                "timeout_seconds": 30,
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }
        )
        test_run = test_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "repo_id": self.repo["id"],
                "project_id": self.project.id,
                "task_id": self.task.id,
                "agent_run_id": agent["id"],
                "patch_application_id": application["id"],
                "test_command_id": command["id"],
                "name": "Python tests",
                "command": command["command"],
                "working_directory": ".",
                "status": "passed",
                "exit_code": 0,
                "stdout_text": "OK",
                "stderr_text": "",
                "combined_output": "OK",
                "duration_ms": 10,
                "timeout_seconds": 30,
                "created_at": now,
                "started_at": now,
                "completed_at": now,
            }
        )
        return agent, application, test_run

    def test_init_status_diff_checkpoint_links_history_and_original_unchanged(self):
        denied = self.client.post(f"/api/git/repos/{self.repo['id']}/init", json={"confirm": False})
        self.assertEqual(denied.status_code, 400)
        initialized = self.initialize()
        initial = initialized["checkpoint"]
        self.assertTrue(initial["commit_sha"])
        self.assertTrue((self.managed / ".git").is_dir())
        self.assertFalse((self.source / ".git").exists())

        status = self.client.get(f"/api/git/repos/{self.repo['id']}/status")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertTrue(status.json()["clean"])
        self.assertEqual(status.json()["changed_files"], [])

        changed_content = b"VALUE = 2\n"
        (self.managed / "app.py").write_bytes(changed_content)
        dirty = self.client.get(f"/api/git/repos/{self.repo['id']}/status").json()
        self.assertFalse(dirty["clean"])
        self.assertEqual(dirty["changed_files"][0]["path"], "app.py")
        diff = self.client.get(f"/api/git/repos/{self.repo['id']}/diff").json()
        self.assertIn("-VALUE = 1", diff["diff"])
        self.assertIn("+VALUE = 2", diff["diff"])
        file_diff = self.client.get(
            f"/api/git/repos/{self.repo['id']}/diff", params={"path": "app.py"}
        )
        self.assertEqual(file_diff.status_code, 200)
        unsafe = self.client.get(
            f"/api/git/repos/{self.repo['id']}/diff", params={"path": "../original/app.py"}
        )
        self.assertEqual(unsafe.status_code, 400)

        agent, application, test_run = self.seed_associations()
        no_confirm = self.client.post(
            f"/api/git/repos/{self.repo['id']}/checkpoints",
            json={"title": "After patch", "confirm": False},
        )
        self.assertEqual(no_confirm.status_code, 400)
        response = self.client.post(
            f"/api/git/repos/{self.repo['id']}/checkpoints",
            json={
                "title": "After patch and tests",
                "message": "Applied patch and passed tests.",
                "task_id": self.task.id,
                "agent_run_id": agent["id"],
                "patch_application_id": application["id"],
                "test_run_id": test_run["id"],
                "confirm": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        checkpoint = response.json()["checkpoint"]
        self.assertEqual(checkpoint["task_id"], self.task.id)
        self.assertEqual(checkpoint["patch_application_id"], application["id"])
        self.assertEqual(checkpoint["test_run_id"], test_run["id"])
        self.assertEqual(checkpoint["agent_run_id"], agent["id"])
        self.assertIn("app.py", [item["path"] for item in checkpoint["changed_files"]])

        workspace_file = file_store.get_file(self.mapping["file_id"])
        expected_hash = hashlib.sha256(changed_content).hexdigest()
        self.assertEqual(workspace_file["sha256"], expected_hash)
        self.assertEqual(repo_store.get_repo_file(self.mapping["id"])["sha256"], expected_hash)
        self.assertEqual(workspace_file["extracted_text"], changed_content.decode())
        self.assertEqual((self.source / "app.py").read_bytes(), self.original_content)

        history = self.client.get(f"/api/git/repos/{self.repo['id']}/checkpoints")
        self.assertEqual(history.json()["total"], 2)
        detail = self.client.get(f"/api/git/checkpoints/{checkpoint['id']}")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.json()["operations"])
        operations = self.client.get(f"/api/git/repos/{self.repo['id']}/operations")
        self.assertGreaterEqual(operations.json()["total"], 4)

    def test_restore_updates_file_metadata_and_marks_indexes_stale(self):
        initialized = self.initialize()
        initial = initialized["checkpoint"]
        now = file_store.now_iso()
        index_store.upsert_index(
            {
                "id": str(uuid.uuid4()),
                "repo_id": self.repo["id"],
                "status": "ready",
                "file_count": 1,
                "indexed_file_count": 1,
                "symbol_count": 1,
                "dependency_count": 0,
                "route_count": 0,
                "metadata": {"symbol_awareness": {"status": "ready"}},
                "created_at": now,
                "updated_at": now,
                "indexed_at": now,
            }
        )
        (self.managed / "app.py").write_text("VALUE = 2\n")
        checkpoint = self.client.post(
            f"/api/git/repos/{self.repo['id']}/checkpoints",
            json={"title": "Value two", "confirm": True},
        ).json()["checkpoint"]
        self.assertNotEqual(initial["commit_sha"], checkpoint["commit_sha"])
        (self.managed / "app.py").write_text("VALUE = 3\n")

        denied = self.client.post(
            f"/api/git/checkpoints/{initial['id']}/restore", json={"confirm": False}
        )
        self.assertEqual(denied.status_code, 400)
        restored = self.client.post(
            f"/api/git/checkpoints/{initial['id']}/restore", json={"confirm": True}
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["checkpoint"]["status"], "restored")
        self.assertEqual((self.managed / "app.py").read_bytes(), self.original_content)
        item = file_store.get_file(self.mapping["file_id"])
        digest = hashlib.sha256(self.original_content).hexdigest()
        self.assertEqual(item["sha256"], digest)
        self.assertEqual(item["extracted_text"], self.original_content.decode())
        self.assertEqual(repo_store.get_repo_file(self.mapping["id"])["sha256"], digest)
        status = self.client.get(f"/api/git/repos/{self.repo['id']}/status").json()
        self.assertFalse(status["clean"])
        self.assertFalse(status["changed_files"][0]["staged"])
        restored_diff = self.client.get(f"/api/git/repos/{self.repo['id']}/diff").json()["diff"]
        self.assertIn("-VALUE = 2", restored_diff)
        self.assertIn("+VALUE = 1", restored_diff)
        stale = index_store.get_index(self.repo["id"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["metadata"]["symbol_awareness"]["status"], "stale")
        self.assertEqual((self.source / "app.py").read_bytes(), self.original_content)

    def test_missing_deleted_unavailable_remote_and_shell_safety(self):
        missing = self.client.get("/api/git/repos/missing/status")
        self.assertEqual(missing.status_code, 404)
        self.initialize()
        (self.managed / "untracked.py").write_text("VALUE = 2\n")
        refused = self.client.post(
            f"/api/git/repos/{self.repo['id']}/checkpoints",
            json={"title": "Untracked file", "confirm": True},
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("untracked files", refused.json()["detail"])
        (self.managed / "untracked.py").unlink()
        repo_store.update_repo(self.repo["id"], {"deleted": True})
        deleted = self.client.get(f"/api/git/repos/{self.repo['id']}/status")
        self.assertEqual(deleted.status_code, 404)
        for args in (
            ["clone", "https://example.com/repo"],
            ["fetch"],
            ["pull"],
            ["push"],
            ["remote", "add", "origin", "x"],
            ["reset", "--hard", "HEAD"],
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate_git_args(args)

        git_paths = [
            route.path
            for route in self.client.app.routes
            if getattr(route, "path", "").startswith("/api/git/")
        ]
        self.assertFalse(
            any(
                operation in path
                for path in git_paths
                for operation in ("clone", "fetch", "pull", "push", "remote", "submodule")
            )
        )

        source = Path("app/services/git/executor.py").read_text()
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        run_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        ]
        self.assertEqual(len(run_calls), 1)
        shell = next(keyword for keyword in run_calls[0].keywords if keyword.arg == "shell")
        self.assertIsInstance(shell.value, ast.Constant)
        self.assertFalse(shell.value.value)

    def test_git_unavailable_and_read_only_agent_chat_context(self):
        with patch("app.services.git.service.git_available", return_value=False):
            status = self.client.get(f"/api/git/repos/{self.repo['id']}/status")
            self.assertEqual(status.status_code, 200)
            self.assertFalse(status.json()["available"])
            unavailable = self.client.post(
                f"/api/git/repos/{self.repo['id']}/init", json={"confirm": True}
            )
            self.assertEqual(unavailable.status_code, 503)

        self.initialize()
        context = GitContextService().context_for_task(self.task.id, self.project.id)
        self.assertIn("Initial workspace checkpoint", context)
        runner_context = AgentRunner()._read_context(
            {
                "id": "other",
                "task_id": self.task.id,
                "project_id": self.project.id,
                "objective": "What checkpoints exist?",
            }
        )
        self.assertIn("Controlled Git context", runner_context)
        with patch("app.services.git.service.run_git") as run:
            reply = GitContextService().answer_for_prompt("What did the last checkpoint include?")
        run.assert_not_called()
        self.assertIn("Latest checkpoint", reply)
        self.assertNotIn("create_checkpoint", Path("app/services/agents/runner.py").read_text())


if __name__ == "__main__":
    unittest.main()
