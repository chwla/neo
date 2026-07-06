import os
import tempfile
import unittest

from pydantic import ValidationError

from app.core.config import get_settings
from app.services.agents import AgentRunCreate, AgentsService
from app.services.agents.store import initialize_agent_tables
from app.services.files import store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import FileLinkCreate
from app.services.notes.store import initialize_notes_tables
from app.services.patches import PatchProposalRequest, PatchProposalService
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.store import initialize_task_tables


class PassiveRunner:
    def start(self, run_id):
        self.run_id = run_id


def valid_patch(_prompt: str) -> str:
    return """# Patch Proposal

## Objective
Improve the implementation.

## Target files
- one.py

## Summary
Return a clearer value.

## Proposed changes
Update the return value.

## Unified diff
```diff
diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
@@ -1 +1 @@
-return 1
+return 2
```

## Risks
Callers may expect the old value.

## Validation needed
Review and test manually.

## Notes
This patch has not been applied."""


class PatchProposalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = f"{self.tmp.name}/files"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        initialize_agent_tables()
        store.initialize_workspace_file_tables()
        self.files = WorkspaceFilesService()
        self.project = ProjectsService().create_project(ProjectCreate(title="Patch project"))
        self.task = TasksService().create_task(
            TaskCreate(title="Change code", project_id=self.project.id)
        )
        self.run = AgentsService(runner=PassiveRunner()).create_run(
            AgentRunCreate(task_id=self.task.id)
        )
        self.service = PatchProposalService(generator=valid_patch)

    def tearDown(self):
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        os.environ.pop("NEO_WORKSPACE_FILES_DIR", None)
        self.tmp.cleanup()

    def upload(self, name="one.py", content=b"return 1\n"):
        return self.files.import_bytes(original_filename=name, content=content)

    def test_one_file_creates_linked_patch_artifact_without_modifying_file(self):
        item = self.upload()
        path = self.files.download_path(item["id"])
        before = path.read_bytes()
        artifact = self.service.propose(
            PatchProposalRequest(
                objective="Return two",
                file_ids=[item["id"]],
                task_id=self.task.id,
                project_id=self.project.id,
                agent_run_id=self.run.id,
            )
        )
        self.assertEqual(artifact["artifact_type"], "patch_proposal")
        self.assertEqual(artifact["task_id"], self.task.id)
        self.assertEqual(artifact["project_id"], self.project.id)
        self.assertEqual(artifact["agent_run_id"], self.run.id)
        self.assertIn("one.py", artifact["content"])
        self.assertIn("diff --git", artifact["content"])
        self.assertIn("This patch has not been applied.", artifact["content"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(store.get_artifact(artifact["id"])["id"], artifact["id"])

    def test_multiple_files_are_recorded_in_metadata(self):
        first = self.upload()
        second = self.upload("two.js", b"export const value = 1;\n")
        artifact = self.service.propose(
            PatchProposalRequest(objective="Update values", file_ids=[first["id"], second["id"]])
        )
        self.assertEqual(artifact["metadata"]["target_filenames"], ["one.py", "two.js"])
        self.assertLessEqual(artifact["metadata"]["context_chars"], 100_000)

    def test_rejects_empty_missing_deleted_and_unsupported_files(self):
        with self.assertRaises(ValidationError):
            PatchProposalRequest(objective="", file_ids=["x"])
        with self.assertRaises(LookupError):
            self.service.propose(PatchProposalRequest(objective="Change", file_ids=["missing"]))

        deleted = self.upload()
        store.update_file(deleted["id"], {"deleted": True})
        with self.assertRaises(LookupError):
            self.service.propose(PatchProposalRequest(objective="Change", file_ids=[deleted["id"]]))

        binary = self.upload("image.png", b"\x89PNG\x00binary")
        with self.assertRaises(ValueError):
            self.service.propose(PatchProposalRequest(objective="Change", file_ids=[binary["id"]]))

    def test_linked_file_count_limit_and_honest_fallback(self):
        for index in range(11):
            item = self.upload(f"file_{index}.py", f"value = {index}\n".encode())
            self.files.attach(item["id"], FileLinkCreate(link_type="task", target_id=self.task.id))
        with self.assertRaisesRegex(ValueError, "at most 10 files"):
            self.service.propose(
                PatchProposalRequest(objective="Change all values", task_id=self.task.id)
            )

        item = store.list_files(limit=1)[0][0]
        fallback = PatchProposalService(generator=lambda _prompt: "I am not sure.").propose(
            PatchProposalRequest(objective="Change safely", file_ids=[item["id"]])
        )
        self.assertEqual(fallback["artifact_type"], "analysis")
        self.assertIn("Could Not Be Generated Reliably", fallback["content"])
        self.assertIn("This patch has not been applied.", fallback["content"])


if __name__ == "__main__":
    unittest.main()
