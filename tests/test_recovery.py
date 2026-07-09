import os
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

import app.services.agents.store as agent_store
import app.services.coding_agent.store as coding_store
import app.services.recovery.store as recovery_store
import app.services.repos.store as repo_store
import app.services.test_runner.store as test_store
from app.core.config import get_settings
from app.main import create_app
from app.services.files.store import initialize_workspace_file_tables
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.recovery.scanner import RecoveryScanner
from app.services.recovery.service import RecoveryService
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.store import initialize_task_tables


class PassiveRunner:
    def __init__(self):
        self.started = []

    def start(self, run_id):
        self.started.append(run_id)


class RecoverySystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "repos")
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        agent_store.initialize_agent_tables()
        initialize_workspace_file_tables()
        coding_store.initialize_coding_agent_tables()
        test_store.initialize_test_runner_tables()
        recovery_store.initialize_recovery_tables()
        self.project = ProjectsService().create_project(ProjectCreate(title="Recovery project"))
        self.task = TasksService().create_task(
            TaskCreate(title="Recover task", description="Recovery test", project_id=self.project.id)
        )
        now = coding_store.now_iso()
        self.repo = repo_store.insert_repo(
            {
                "id": str(uuid.uuid4()),
                "project_id": self.project.id,
                "name": "recovery-repo",
                "original_path": str(self.root / "original"),
                "workspace_path": str(self.root / "repo"),
                "status": "ready",
                "file_count": 1,
                "indexed_file_count": 0,
                "total_bytes": 0,
                "metadata": {},
                "deleted": False,
                "created_at": now,
                "updated_at": now,
                "indexed_at": None,
            }
        )

    def tearDown(self):
        get_settings.cache_clear()
        for name in ("NEO_DATABASE_URL", "NEO_WORKSPACE_FILES_DIR", "NEO_WORKSPACE_REPOS_DIR"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def _agent_run(self, status="queued"):
        now = agent_store.now_iso()
        run = agent_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "task_id": self.task.id,
                "project_id": self.project.id,
                "title": "Recovery agent",
                "objective": "Recover safely",
                "status": status,
                "mode": "assist",
                "plan": [],
                "final_output": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": now if status != "queued" else None,
                "completed_at": None,
                "cancelled_at": None,
                "forked_from_run_id": None,
            }
        )
        agent_store.insert_step(
            {
                "id": str(uuid.uuid4()),
                "run_id": run["id"],
                "step_index": 0,
                "step_type": "plan",
                "title": "Plan",
                "status": "running" if status in {"planning", "running"} else "pending",
                "input": {},
                "output_text": None,
                "error": None,
                "requires_approval": False,
                "approval_status": None,
                "created_at": now,
                "updated_at": now,
                "started_at": now,
                "completed_at": None,
            }
        )
        return run

    def _coding_run(self, status="waiting_patch_approval", action_status="pending"):
        now = coding_store.now_iso()
        agent = self._agent_run(status="waiting_approval")
        run = coding_store.insert_run(
            {
                "id": str(uuid.uuid4()),
                "agent_run_id": agent["id"],
                "task_id": self.task.id,
                "project_id": self.project.id,
                "repo_id": self.repo["id"],
                "objective": "Patch safely",
                "status": status,
                "current_iteration": 1,
                "max_iterations": 3,
                "selected_files": [{"file_id": "file-1", "relative_path": "app.py", "reason": "test"}],
                "patch_artifact_id": "artifact-1",
                "patch_application_id": None,
                "test_run_id": None,
                "checkpoint_id": None,
                "error": None,
                "metadata": {"resolved_rules": {}, "applied_profiles": [], "rule_warnings": []},
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "cancelled_at": None,
                "forked_from_run_id": None,
                "recovery_state": "active",
                "last_recoverable_at": now,
            }
        )
        action = coding_store.insert_action(
            {
                "id": str(uuid.uuid4()),
                "coding_run_id": run["id"],
                "agent_run_id": agent["id"],
                "action_type": "apply_patch",
                "status": action_status,
                "title": "Approve and apply patch",
                "description": "Approval gate.",
                "payload": {"artifact_id": "artifact-1", "target_files": [], "atomic": True},
                "result": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "decided_at": None,
                "executed_at": None,
            }
        )
        return run, action

    def test_pending_patch_approval_survives_resume_without_execution(self):
        run, action = self._coding_run()
        service = RecoveryService(runner=PassiveRunner())
        result = service.resume("coding_agent", run["id"], confirm=True)
        self.assertEqual(result["summary"].status, "waiting_patch_approval")
        self.assertEqual(coding_store.get_action(action["id"])["status"], "pending")
        self.assertEqual(result["summary"].pending_action["id"], action["id"])
        events, _ = recovery_store.list_events(run_type="coding_agent", run_id=run["id"])
        self.assertEqual(events[0]["event_type"], "resumed")

    def test_scanner_detects_stuck_agent_and_repairs_executing_action(self):
        agent = self._agent_run(status="running")
        coding, action = self._coding_run(status="applying_patch", action_status="executing")
        result = RecoveryScanner().scan()
        self.assertGreaterEqual(result["agent_runs"], 1)
        self.assertEqual(agent_store.get_run(agent["id"])["status"], "interrupted")
        self.assertEqual(coding_store.get_run(coding["id"])["status"], "needs_review")
        self.assertEqual(coding_store.get_action(action["id"])["status"], "pending")
        events, _ = recovery_store.list_events(run_type="coding_agent", run_id=coding["id"])
        self.assertTrue(any(item["event_type"] == "state_repaired" for item in events))

    def test_completed_and_cancelled_cannot_resume_but_can_fork_agent(self):
        completed = self._agent_run(status="completed")
        cancelled = self._agent_run(status="cancelled")
        service = RecoveryService(runner=PassiveRunner())
        with self.assertRaises(ValueError):
            service.resume("agent", completed["id"], confirm=True)
        with self.assertRaises(ValueError):
            service.resume("agent", cancelled["id"], confirm=True)
        forked = service.fork("agent", completed["id"], confirm=True)
        self.assertEqual(forked["summary"].forked_from_run_id, completed["id"])
        self.assertEqual(forked["summary"].status, "queued")

    def test_repair_state_validates_safe_targets(self):
        run, _action = self._coding_run(status="needs_review")
        service = RecoveryService(runner=PassiveRunner())
        fixed = service.repair_state(
            "coding_agent", run["id"], confirm=True, target_status="waiting_patch_approval"
        )
        self.assertEqual(fixed["summary"].status, "waiting_patch_approval")
        with self.assertRaises(ValueError):
            service.repair_state("coding_agent", run["id"], confirm=True, target_status="applying_patch")

    def test_retry_failed_test_creates_pending_test_approval_only(self):
        run, old_action = self._coding_run(status="failed")
        coding_store.update_action(
            old_action["id"], {"status": "rejected", "updated_at": coding_store.now_iso()}
        )
        now = test_store.now_iso()
        command = test_store.insert_command(
            {
                "id": str(uuid.uuid4()),
                "repo_id": self.repo["id"],
                "project_id": self.project.id,
                "name": "Unit tests",
                "command": ["python", "-m", "pytest"],
                "working_directory": ".",
                "timeout_seconds": 120,
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
                "agent_run_id": run["agent_run_id"],
                "patch_application_id": None,
                "test_command_id": command["id"],
                "name": "Unit tests",
                "command": command["command"],
                "working_directory": ".",
                "status": "failed",
                "exit_code": 1,
                "stdout_text": "",
                "stderr_text": "",
                "combined_output": "failed",
                "duration_ms": 1,
                "timeout_seconds": 120,
                "error": None,
                "metadata": {},
                "created_at": now,
                "started_at": now,
                "completed_at": now,
            }
        )
        coding_store.update_run(run["id"], {"test_run_id": test_run["id"], "updated_at": now})
        result = RecoveryService(runner=PassiveRunner()).retry(
            "coding_agent", run["id"], confirm=True
        )
        self.assertEqual(result["summary"].status, "waiting_test_approval")
        pending = result["summary"].pending_action
        self.assertEqual(pending["action_type"], "run_tests")
        self.assertEqual(pending["payload"]["test_commands"][0]["id"], command["id"])

    def test_api_and_chatbot_recovery_summary_are_read_only(self):
        run, action = self._coding_run()
        client = TestClient(create_app())
        response = client.get(f"/api/recovery/runs/coding_agent/{run['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["summary"]["pending_action"]["id"], action["id"])
        answer = RecoveryService(runner=PassiveRunner()).answer_for_prompt("What is this run waiting for?")
        self.assertIn("read-only", answer)
        self.assertIn("Waiting for", answer)
        self.assertEqual(coding_store.get_action(action["id"])["status"], "pending")


if __name__ == "__main__":
    unittest.main()
