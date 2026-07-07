import ast
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
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate
from app.services.patch_apply import store as patch_store
from app.services.projects import ProjectCreate, ProjectsService
from app.services.repos import store as repo_store
from app.services.tasks import TaskCreate, TasksService
from app.services.test_runner.executor import (
    STDOUT_LIMIT,
    ExecutionResult,
    _read_limited,
    execute,
)
from app.services.test_runner.service import TestRunnerContextService

PASSING_TEST = """
import sys
import unittest

class SampleTest(unittest.TestCase):
    def test_output(self):
        print("runner stdout marker")
        print("runner stderr marker", file=sys.stderr)
        self.assertEqual(2 + 2, 4)
"""

FAILING_TEST = """
import unittest

class SampleTest(unittest.TestCase):
    def test_failure(self):
        self.assertEqual("actual", "expected")
"""

SLOW_TEST = """
import time
import unittest

class SampleTest(unittest.TestCase):
    def test_slow(self):
        time.sleep(5)
"""


class ControlledTestRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "workspace-files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "workspace-repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(ProjectCreate(title="Runner project"))
        self.task = TasksService().create_task(
            TaskCreate(title="Validate change", project_id=self.project.id)
        )
        self.source = self.root / "original"
        (self.source / "tests").mkdir(parents=True)
        (self.source / "tests" / "test_sample.py").write_text(PASSING_TEST)
        (self.source / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
        (self.source / "package.json").write_text(
            '{"scripts":{"test":"node --test","build":"vite build","lint":"eslint ."}}'
        )
        response = self.client.post(
            "/api/repos/register",
            json={
                "path": str(self.source),
                "project_id": self.project.id,
                "confirm": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.repo = response.json()["repo"]
        self.managed = Path(repo_store.get_repo(self.repo["id"])["workspace_path"])

    def tearDown(self):
        get_settings.cache_clear()
        for name in ("NEO_DATABASE_URL", "NEO_WORKSPACE_FILES_DIR", "NEO_WORKSPACE_REPOS_DIR"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def create_command(self, command=None, **overrides):
        payload = {
            "name": "Python unittest",
            "command": command or ["python", "-m", "unittest", "discover", "-s", "tests", "-v"],
            "working_directory": ".",
            "timeout_seconds": 30,
            **overrides,
        }
        response = self.client.post(
            f"/api/test-runner/repos/{self.repo['id']}/commands", json=payload
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["command"]

    def run_command(self, command_id, **links):
        return self.client.post(
            f"/api/test-runner/commands/{command_id}/run", json={"confirm": True, **links}
        )

    def test_create_detect_list_update_disable_and_safety_rejections(self):
        command = self.create_command()
        listed = self.client.get(f"/api/test-runner/repos/{self.repo['id']}/commands")
        self.assertEqual(listed.json()["commands"][0]["command"][0], "python")
        updated = self.client.patch(
            f"/api/test-runner/commands/{command['id']}", json={"timeout_seconds": 20}
        )
        self.assertEqual(updated.json()["command"]["timeout_seconds"], 20)

        detected = self.client.post(f"/api/test-runner/repos/{self.repo['id']}/detect")
        argv = [item["command"] for item in detected.json()["suggestions"]]
        self.assertIn(["python", "-m", "pytest", "-q"], argv)
        self.assertIn(["npm", "test"], argv)
        self.assertIn(["npm", "run", "build"], argv)
        self.assertIn(["npm", "run", "lint"], argv)

        unsafe = [
            ["python", "-m", "pytest", "&&", "rm", "-rf", "."],
            ["git", "status"],
            ["rm", "-rf", "."],
            ["npm", "install"],
            ["bash", "-c", "pytest"],
            ["curl", "https://example.com"],
            ["python", "-m", "pytest", "/tmp/outside_tests"],
            ["python", "-m", "pytest", "../outside_tests"],
        ]
        for argv_item in unsafe:
            with self.subTest(argv=argv_item):
                response = self.client.post(
                    f"/api/test-runner/repos/{self.repo['id']}/commands",
                    json={
                        "name": "unsafe",
                        "command": argv_item,
                        "working_directory": ".",
                        "timeout_seconds": 10,
                    },
                )
                self.assertEqual(response.status_code, 400, response.text)

        escaped = self.client.post(
            f"/api/test-runner/repos/{self.repo['id']}/commands",
            json={
                "name": "escape",
                "command": ["python", "-m", "pytest"],
                "working_directory": "../original",
                "timeout_seconds": 10,
            },
        )
        self.assertEqual(escaped.status_code, 400)
        outside = self.root / "outside"
        outside.mkdir()
        (self.managed / "escape-link").symlink_to(outside, target_is_directory=True)
        symlink = self.client.post(
            f"/api/test-runner/repos/{self.repo['id']}/commands",
            json={
                "name": "symlink",
                "command": ["python", "-m", "pytest"],
                "working_directory": "escape-link",
                "timeout_seconds": 10,
            },
        )
        self.assertEqual(symlink.status_code, 400)

        disabled = self.client.delete(f"/api/test-runner/commands/{command['id']}")
        self.assertFalse(disabled.json()["command"]["enabled"])
        denied = self.run_command(command["id"])
        self.assertEqual(denied.status_code, 400)

    def test_passing_failing_output_history_confirmation_and_original_unchanged(self):
        original = (self.source / "tests" / "test_sample.py").read_bytes()
        command = self.create_command()
        denied = self.client.post(
            f"/api/test-runner/commands/{command['id']}/run", json={"confirm": False}
        )
        self.assertEqual(denied.status_code, 400)
        passed = self.run_command(command["id"])
        self.assertEqual(passed.status_code, 200, passed.text)
        run = passed.json()["run"]
        self.assertEqual(run["status"], "passed")
        self.assertEqual(run["exit_code"], 0)
        self.assertGreaterEqual(run["duration_ms"], 0)
        self.assertIn("runner stdout marker", run["stdout_text"])
        self.assertIn("runner stderr marker", run["stderr_text"])
        self.assertEqual(run["repo_id"], self.repo["id"])

        (self.managed / "tests" / "test_sample.py").write_text(FAILING_TEST)
        failed = self.run_command(command["id"]).json()["run"]
        self.assertEqual(failed["status"], "failed")
        self.assertNotEqual(failed["exit_code"], 0)
        self.assertIn("AssertionError", failed["combined_output"])
        history = self.client.get("/api/test-runner/runs", params={"repo_id": self.repo["id"]})
        self.assertEqual(history.json()["total"], 2)
        detail = self.client.get(f"/api/test-runner/runs/{failed['id']}")
        self.assertEqual(detail.json()["run"]["status"], "failed")
        restarted = TestClient(create_app())
        persisted = restarted.get(f"/api/test-runner/runs/{failed['id']}")
        self.assertEqual(persisted.json()["run"]["exit_code"], failed["exit_code"])
        self.assertEqual((self.source / "tests" / "test_sample.py").read_bytes(), original)

    def test_timeout_and_output_truncation(self):
        (self.managed / "tests" / "test_sample.py").write_text(SLOW_TEST)
        command = self.create_command(timeout_seconds=1)
        timed_out = self.run_command(command["id"]).json()["run"]
        self.assertEqual(timed_out["status"], "timed_out")
        self.assertIsNone(timed_out["exit_code"])
        self.assertLess(timed_out["duration_ms"], 4000)

        fake = ExecutionResult(
            status="passed",
            exit_code=0,
            stdout_text="x",
            stderr_text="y",
            combined_output="x\ny",
            duration_ms=1,
            error=None,
            metadata={
                "stdout_truncated": True,
                "stderr_truncated": False,
                "combined_truncated": True,
            },
        )
        with patch("app.services.test_runner.service.execute", return_value=fake):
            stored = self.run_command(command["id"]).json()["run"]
        self.assertTrue(stored["metadata"]["stdout_truncated"])
        self.assertTrue(stored["metadata"]["combined_truncated"])

        with tempfile.TemporaryFile() as output:
            output.write(b"x" * (STDOUT_LIMIT + 20))
            text, truncated = _read_limited(output, STDOUT_LIMIT)
        self.assertEqual(len(text), STDOUT_LIMIT)
        self.assertTrue(truncated)

    def test_task_agent_patch_links_and_read_only_context(self):
        now = agent_store.now_iso()
        agent = agent_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "task_id": self.task.id,
                "project_id": self.project.id,
                "title": "Runner context",
                "objective": "Inspect tests",
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
        mapping = repo_store.list_repo_files(self.repo["id"])[0][0]
        artifact = WorkspaceFilesService().create_artifact(
            ArtifactCreate(
                title="Applied patch",
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
                "file_id": mapping["file_id"],
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
        command = self.create_command()
        response = self.run_command(
            command["id"],
            task_id=self.task.id,
            agent_run_id=agent["id"],
            patch_application_id=application["id"],
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["run"]
        self.assertEqual(run["task_id"], self.task.id)
        self.assertEqual(run["agent_run_id"], agent["id"])
        self.assertEqual(run["patch_application_id"], application["id"])

        context = TestRunnerContextService().context_for_task(self.task.id, self.project.id)
        self.assertIn("Stored test results", context)
        self.assertIn("Suggested validation", context)
        runner_context = AgentRunner()._read_context(
            {
                "id": "other",
                "task_id": self.task.id,
                "project_id": self.project.id,
                "objective": "Did the patch pass tests?",
            }
        )
        self.assertIn("Controlled test runner context", runner_context)
        reply = TestRunnerContextService().answer_for_prompt("What failed in the last test run?")
        self.assertIn("Latest stored test run", reply)

    def test_no_shell_true_and_minimal_environment(self):
        source = Path("app/services/test_runner/executor.py").read_text()
        tree = ast.parse(source)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        popen_calls = [
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"
        ]
        self.assertEqual(len(popen_calls), 1)
        shell = next(keyword for keyword in popen_calls[0].keywords if keyword.arg == "shell")
        self.assertIsInstance(shell.value, ast.Constant)
        self.assertFalse(shell.value.value)
        with patch("app.services.test_runner.executor.subprocess.Popen") as popen:
            process = popen.return_value
            process.wait.return_value = 0
            process.pid = 123
            execute(["python", "-m", "unittest"], self.managed, 5)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(set(environment), {"PATH", "HOME", "CI", "NO_COLOR", "PYTHONUNBUFFERED"})
        self.assertIs(popen.call_args.kwargs["stdin"], __import__("subprocess").DEVNULL)


if __name__ == "__main__":
    unittest.main()
