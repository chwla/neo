import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.code_index import store as code_index_store
from app.services.code_index.service import CodeIndexService
from app.services.coding_agent.orchestrator import CodingAgentOrchestrator
from app.services.coding_agent.service import CodingAgentService
from app.services.coding_agent.types import CodingRunCreate
from app.services.files import store as file_store
from app.services.git.service import GitService
from app.services.git.types import CheckpointRestoreRequest, GitInitRequest
from app.services.patches.service import PatchProposalService
from app.services.projects import ProjectCreate, ProjectsService
from app.services.repos import store as repo_store
from app.services.symbol_awareness import store as symbol_store
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.tasks import TaskCreate, TasksService
from app.services.test_runner.service import TestRunnerService
from app.services.test_runner.types import TestCommandCreate as SavedTestCommandCreate


class SimpleTaskPlanner:
    def resolve(self, objective, task_id, project_id):
        if task_id:
            return TasksService().get_task(task_id), project_id, []
        parent = TasksService().create_task(
            TaskCreate(
                title=objective[:80],
                description=objective,
                project_id=project_id,
                status="doing",
                tags=["agent", "auto-created"],
            )
        )
        child = TasksService().create_task(
            TaskCreate(
                title="Validate implementation",
                description="Review the managed coding-agent result.",
                project_id=project_id,
                parent_task_id=parent.id,
                tags=["agent", "subtask"],
            )
        )
        return parent, project_id, [child]


class MultiStepCodingAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(
            ProjectCreate(title="Coding project", description="A small Python project")
        )
        self.task = TasksService().create_task(
            TaskCreate(
                title="Increment value",
                description="Change VALUE from one to two.",
                project_id=self.project.id,
                status="doing",
            )
        )
        self.source = self.root / "original"
        (self.source / "tests_pass").mkdir(parents=True)
        (self.source / "tests_fail").mkdir()
        (self.source / "calc.py").write_text("VALUE = 1\n\ndef current():\n    return VALUE\n")
        (self.source / "tests_pass" / "test_ok.py").write_text(
            "import unittest\n\nclass OK(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n"
        )
        (self.source / "tests_fail" / "test_bad.py").write_text(
            "import unittest\n\nclass Bad(unittest.TestCase):\n"
            "    def test_bad(self):\n        self.assertEqual(1, 2)\n"
        )
        self.original_hash = self._source_hash()
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
        CodeIndexService().build(self.repo["id"], self._index_request())
        SymbolAwarenessService().build(self.repo["id"], self._symbol_request())
        _git_state, self.initial_checkpoint = GitService().initialize(
            self.repo["id"], GitInitRequest(confirm=True)
        )
        mapping, _ = repo_store.list_repo_files(self.repo["id"], q="calc.py")
        self.calc_mapping = mapping[0]
        self.patch_service = PatchProposalService(generator=self._generate_patch)

    def tearDown(self):
        get_settings.cache_clear()
        for name in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
        ):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    @staticmethod
    def _index_request():
        from app.services.code_index.types import CodeIndexBuildRequest

        return CodeIndexBuildRequest(force=True, summarize=False)

    @staticmethod
    def _symbol_request():
        from app.services.symbol_awareness.types import SymbolAwarenessBuildRequest

        return SymbolAwarenessBuildRequest(force=True)

    def _source_hash(self):
        digest = hashlib.sha256()
        for path in sorted(self.source.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(self.source).as_posix().encode())
                digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _generate_patch(prompt):
        old, new = (
            ("VALUE = 2", "VALUE = 3") if "VALUE = 2" in prompt else ("VALUE = 1", "VALUE = 2")
        )
        diff_blank = " "
        return f"""# Patch Proposal

## Objective
Update the value.

## Unified diff
```diff
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,4 +1,4 @@
-{old}
+{new}
{diff_blank}
 def current():
     return VALUE
```

## Notes
This patch has not been applied."""

    def orchestrator(self):
        return CodingAgentOrchestrator(
            task_planner=SimpleTaskPlanner(), patch_service=self.patch_service
        )

    def create_command(self, name, directory):
        return TestRunnerService().create_command(
            self.repo["id"],
            SavedTestCommandCreate(
                name=name,
                command=["python", "-m", "unittest", "discover", "-s", directory, "-v"],
                working_directory=".",
                timeout_seconds=30,
                project_id=self.project.id,
            ),
        )

    def start(self, *, max_iterations=3, task=True):
        return self.orchestrator().start(
            CodingRunCreate(
                objective="Increment VALUE in calc.py",
                task_id=self.task.id if task else None,
                project_id=self.project.id,
                repo_id=self.repo["id"],
                max_iterations=max_iterations,
            )
        )

    def apply_options(self, detail):
        targets = detail["current_action_request"]["payload"]["target_files"]
        target = next(item for item in targets if item["file_id"] == self.calc_mapping["file_id"])
        return {"file_id": target["file_id"]}

    def test_start_selects_indexed_symbol_context_and_waits_without_applying(self):
        detail = self.start()
        run = detail["coding_run"]
        self.assertEqual(run["status"], "waiting_patch_approval")
        self.assertEqual(detail["agent_run"]["status"], "waiting_approval")
        self.assertEqual(detail["current_action_request"]["action_type"], "apply_patch")
        self.assertEqual(detail["patch_artifact"]["artifact_type"], "patch_proposal")
        self.assertIn("This patch has not been applied.", detail["patch_artifact"]["content"])
        self.assertTrue(
            {item["source"] for item in run["selected_files"]} & {"symbol_awareness", "code_index"}
        )
        managed = Path(repo_store.get_repo(self.repo["id"])["workspace_path"])
        self.assertIn("VALUE = 1", (managed / "calc.py").read_text())
        self.assertEqual(self._source_hash(), self.original_hash)
        self.assertEqual(
            file_store.get_artifact(run["patch_artifact_id"])["agent_run_id"], run["agent_run_id"]
        )

    def test_objective_without_task_creates_parent_and_subtask(self):
        detail = self.start(task=False)
        run = detail["coding_run"]
        self.assertIsNotNone(run["task_id"])
        self.assertNotEqual(run["task_id"], self.task.id)
        self.assertTrue(run["metadata"]["created_subtask_ids"])

    def test_approved_patch_test_and_checkpoint_complete_with_links(self):
        command = self.create_command("Passing unittest", "tests_pass")
        orchestrator = self.orchestrator()
        detail = orchestrator.start(
            CodingRunCreate(
                objective="Increment VALUE in calc.py",
                task_id=self.task.id,
                project_id=self.project.id,
                repo_id=self.repo["id"],
            )
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options=self.apply_options(detail),
        )
        self.assertEqual(detail["coding_run"]["status"], "waiting_test_approval")
        self.assertEqual(detail["patch_application"]["status"], "applied")
        action = detail["current_action_request"]
        self.assertEqual(action["action_type"], "run_tests")
        detail = orchestrator.approve(
            action["id"], confirm=True, options={"test_command_id": command["id"]}
        )
        self.assertEqual(detail["test_run"]["status"], "passed")
        self.assertEqual(detail["coding_run"]["status"], "waiting_checkpoint_approval")
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options={},
        )
        self.assertEqual(detail["coding_run"]["status"], "completed")
        self.assertIsNotNone(detail["checkpoint"])
        self.assertEqual(detail["checkpoint"]["task_id"], self.task.id)
        self.assertEqual(
            detail["checkpoint"]["patch_application_id"], detail["patch_application"]["id"]
        )
        self.assertEqual(detail["checkpoint"]["test_run_id"], detail["test_run"]["id"])
        self.assertIn("Patch applied: yes", detail["agent_run"]["final_output"])
        self.assertIn("Files changed: calc.py", detail["agent_run"]["final_output"])
        self.assertNotIn("tests_pass/test_ok.py", detail["agent_run"]["final_output"])
        self.assertEqual(self._source_hash(), self.original_hash)

    def test_multi_file_apply_and_final_summary_use_applied_audit_rows(self):
        command = self.create_command("Passing unittest", "tests_pass")
        proposal = """# Patch Proposal

## Unified diff
```diff
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,1 +1,1 @@
-VALUE = 1
+VALUE = 2
diff --git a/helper.py b/helper.py
new file mode 100644
--- /dev/null
+++ b/helper.py
@@ -0,0 +1,2 @@
+def helper():
+    return 2
```

## Notes
This patch has not been applied."""
        orchestrator = CodingAgentOrchestrator(
            task_planner=SimpleTaskPlanner(),
            patch_service=PatchProposalService(generator=lambda _prompt: proposal),
        )
        detail = orchestrator.start(
            CodingRunCreate(
                objective="Update calc and add helper",
                task_id=self.task.id,
                project_id=self.project.id,
                repo_id=self.repo["id"],
            )
        )
        self.assertTrue(detail["current_action_request"]["payload"]["atomic"])
        detail = orchestrator.approve(
            detail["current_action_request"]["id"], confirm=True, options={}
        )
        self.assertEqual(
            {item["relative_path"] for item in detail["patch_application"]["files"]},
            {"calc.py", "helper.py"},
        )
        self.assertEqual(code_index_store.get_index(self.repo["id"])["status"], "stale")
        self.assertEqual(symbol_store.get_status(self.repo["id"])["status"], "stale")
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options={"test_command_id": command["id"]},
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"], confirm=True, options={}
        )
        self.assertEqual(
            detail["coding_run"]["status"],
            "completed",
            [item for item in detail["action_requests"] if item["status"] == "failed"],
        )
        self.assertIn("Files changed: calc.py, helper.py", detail["agent_run"]["final_output"])
        self.assertNotIn("tests_pass/test_ok.py", detail["agent_run"]["final_output"])
        managed = Path(repo_store.get_repo(self.repo["id"])["workspace_path"])
        self.assertTrue((managed / "helper.py").is_file())
        GitService().restore(
            self.initial_checkpoint["id"], CheckpointRestoreRequest(confirm=True)
        )
        self.assertFalse((managed / "helper.py").exists())
        self.assertIn("VALUE = 1", (managed / "calc.py").read_text())
        self.assertEqual(self._source_hash(), self.original_hash)

    def test_failed_test_proposes_followup_and_max_iterations_stops(self):
        command = self.create_command("Failing unittest", "tests_fail")
        orchestrator = self.orchestrator()
        detail = orchestrator.start(
            CodingRunCreate(
                objective="Increment VALUE in calc.py",
                task_id=self.task.id,
                project_id=self.project.id,
                repo_id=self.repo["id"],
                max_iterations=2,
            )
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options=self.apply_options(detail),
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options={"test_command_id": command["id"]},
        )
        self.assertEqual(detail["coding_run"]["current_iteration"], 2)
        self.assertEqual(detail["coding_run"]["status"], "waiting_patch_approval")
        self.assertEqual(detail["current_action_request"]["action_type"], "apply_patch")
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options=self.apply_options(detail),
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options={"test_command_id": command["id"]},
        )
        self.assertEqual(detail["coding_run"]["status"], "failed")
        self.assertIn("maximum iteration", detail["coding_run"]["error"])

    def test_reject_revise_stale_failure_skip_and_cancel_are_explicit(self):
        orchestrator = self.orchestrator()
        detail = self.start()
        rejected = orchestrator.reject(
            detail["current_action_request"]["id"], "Keep the change smaller."
        )
        self.assertIsNone(rejected["patch_application"])
        self.assertEqual(rejected["current_action_request"]["action_type"], "revise_patch")
        revised = orchestrator.revise(detail["coding_run"]["id"], "Only change calc.py.")
        self.assertEqual(revised["current_action_request"]["action_type"], "apply_patch")
        cancelled = orchestrator.cancel(revised["coding_run"]["id"])
        self.assertEqual(cancelled["coding_run"]["status"], "cancelled")
        self.assertTrue(cancelled["steps"])

    def test_stale_patch_is_rejected_safely_and_offers_revision(self):
        orchestrator = self.orchestrator()
        detail = self.start()
        file_store.update_file(
            self.calc_mapping["file_id"],
            {"sha256": "0" * 64, "updated_at": file_store.now_iso()},
        )
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options=self.apply_options(detail),
        )
        self.assertEqual(detail["coding_run"]["status"], "waiting_patch_approval")
        self.assertEqual(detail["current_action_request"]["action_type"], "revise_patch")
        failed = [item for item in detail["action_requests"] if item["status"] == "failed"]
        self.assertTrue(failed)
        self.assertIsNone(detail["patch_application"])
        self.assertEqual(self._source_hash(), self.original_hash)

    def test_multi_context_single_file_diff_is_normalized_and_analysis_is_revisable(self):
        headerless = """# Patch Proposal

## Unified diff
```diff
--- a/calc.py
+++ b/calc.py
@@ -1,1 +1,1 @@
-VALUE = 1
+VALUE = 2
```

## Notes
This patch has not been applied."""
        normalized = CodingAgentOrchestrator(
            task_planner=SimpleTaskPlanner(),
            patch_service=PatchProposalService(generator=lambda _prompt: headerless),
        ).start(
            CodingRunCreate(
                objective="Increment VALUE in calc.py",
                task_id=self.task.id,
                project_id=self.project.id,
                repo_id=self.repo["id"],
            )
        )
        self.assertEqual(normalized["patch_artifact"]["artifact_type"], "patch_proposal")
        self.assertIn("diff --git a/calc.py b/calc.py", normalized["patch_artifact"]["content"])

        analysis = CodingAgentOrchestrator(
            task_planner=SimpleTaskPlanner(),
            patch_service=PatchProposalService(generator=lambda _prompt: "No reliable diff."),
        ).start(
            CodingRunCreate(
                objective="Increment VALUE in calc.py",
                task_id=self.task.id,
                project_id=self.project.id,
                repo_id=self.repo["id"],
            )
        )
        self.assertEqual(analysis["coding_run"]["status"], "waiting_patch_approval")
        self.assertEqual(analysis["patch_artifact"]["artifact_type"], "analysis")
        self.assertEqual(analysis["current_action_request"]["action_type"], "revise_patch")
        self.assertIsNone(analysis["patch_application"])

    def test_skip_tests_and_checkpoint_complete_without_execution(self):
        orchestrator = self.orchestrator()
        detail = self.start()
        detail = orchestrator.approve(
            detail["current_action_request"]["id"],
            confirm=True,
            options=self.apply_options(detail),
        )
        self.assertEqual(detail["current_action_request"]["action_type"], "skip_tests")
        detail = orchestrator.approve(
            detail["current_action_request"]["id"], confirm=True, options={}
        )
        self.assertIsNone(detail["test_run"])
        checkpoint_action = detail["current_action_request"]
        detail = orchestrator.reject(checkpoint_action["id"], "No checkpoint needed.")
        self.assertEqual(detail["current_action_request"]["action_type"], "skip_checkpoint")
        detail = orchestrator.approve(
            detail["current_action_request"]["id"], confirm=True, options={}
        )
        self.assertEqual(detail["coding_run"]["status"], "completed")
        self.assertIsNone(detail["checkpoint"])

    def test_api_confirmation_and_read_only_chat_summary(self):
        orchestrator = self.orchestrator()
        detail = self.start()
        action = detail["current_action_request"]
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            orchestrator.approve(action["id"], confirm=False, options={})
        response = self.client.get(f"/api/coding-agent/runs/{detail['coding_run']['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get("/api/coding-agent/runs", params={"task_id": self.task.id})
        self.assertEqual(listed.status_code, 200)
        self.assertGreaterEqual(listed.json()["total"], 1)
        answer = CodingAgentService(orchestrator).answer_for_prompt(
            "What is the current coding run waiting for?"
        )
        self.assertIn("read-only", answer)
        self.assertIn("Approve and apply patch", answer)
        self.assertIsNone(orchestrator.detail(detail["coding_run"]["id"])["patch_application"])


if __name__ == "__main__":
    unittest.main()
