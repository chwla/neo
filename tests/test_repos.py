import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents.runner import AgentRunner
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import FileLinkCreate
from app.services.patch_apply import (
    ControlledPatchApplyService,
    PatchApplyRequest,
    PatchValidateRequest,
)
from app.services.patches import PatchProposalRequest, PatchProposalService
from app.services.projects import ProjectCreate, ProjectsService
from app.services.repos import store as repo_store
from app.services.repos.safety import validate_repo_root
from app.services.repos.scanner import scan_repo
from app.services.tasks import TaskCreate, TasksService


class RepoWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "workspace-files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "workspace-repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(ProjectCreate(title="Repo project"))
        self.task = TasksService().create_task(
            TaskCreate(title="Inspect repo", project_id=self.project.id)
        )
        self.source = self.root / "source-project"
        (self.source / "app").mkdir(parents=True)
        (self.source / "app" / "main.py").write_text(
            'def greeting(name):\n    return "Hello " + name\n', encoding="utf-8"
        )
        (self.source / "README.md").write_text("# Example\nneedle documentation\n")
        (self.source / ".git").mkdir()
        (self.source / ".git" / "config").write_text("secret git metadata")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "dependency.js").write_text("ignored")
        (self.source / ".env").write_text("TOKEN=do-not-copy")
        (self.source / "logo.png").write_bytes(b"\x89PNG\x00binary")
        outside = self.root / "outside.txt"
        outside.write_text("must not follow")
        (self.source / "outside-link.txt").symlink_to(outside)

    def tearDown(self):
        get_settings.cache_clear()
        for name in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
        ):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def register(self) -> dict:
        response = self.client.post(
            "/api/repos/register",
            json={
                "path": str(self.source),
                "project_id": self.project.id,
                "confirm": True,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["repo"]

    def test_register_copy_ignore_search_project_context_and_persistence(self):
        denied = self.client.post(
            "/api/repos/register", json={"path": str(self.source), "confirm": False}
        )
        self.assertEqual(denied.status_code, 400)
        repo = self.register()
        self.assertEqual(repo["status"], "ready")
        self.assertEqual(repo["file_count"], 2)
        self.assertGreaterEqual(repo["metadata"]["ignored_files"], 2)
        self.assertGreaterEqual(repo["metadata"]["ignored_dirs"], 2)
        register_payload = self.client.get(f"/api/repos/{repo['id']}").json()
        self.assertEqual(register_payload["stats"]["indexed_file_count"], 2)

        stored = repo_store.get_repo(repo["id"])
        managed = Path(stored["workspace_path"])
        self.assertEqual(
            (managed / "app" / "main.py").read_text(), (self.source / "app" / "main.py").read_text()
        )
        self.assertFalse((managed / ".git").exists())
        self.assertFalse((managed / "node_modules").exists())
        self.assertFalse((managed / ".env").exists())
        self.assertFalse((managed / "logo.png").exists())
        self.assertFalse((managed / "outside-link.txt").exists())

        listed = self.client.get(f"/api/repos/{repo['id']}/files").json()
        self.assertEqual(listed["total"], 2)
        paths = {item["relative_path"] for item in listed["files"]}
        self.assertEqual(paths, {"README.md", "app/main.py"})
        searched = self.client.get(f"/api/repos/{repo['id']}/files", params={"q": "needle"}).json()
        self.assertEqual(searched["total"], 1)

        main_mapping = next(
            item for item in listed["files"] if item["relative_path"] == "app/main.py"
        )
        detail = self.client.get(f"/api/repos/{repo['id']}/files/{main_mapping['id']}").json()
        self.assertEqual(detail["file"]["metadata"]["relative_path"], "app/main.py")
        self.assertEqual(detail["file"]["metadata"]["original_path"], str(self.source.resolve()))
        self.assertEqual(detail["file"]["metadata"]["repo_name"], "source-project")
        project_files = self.client.get("/api/files", params={"project_id": self.project.id}).json()
        self.assertEqual(project_files["total"], 2)

        files = WorkspaceFilesService()
        files.attach(
            main_mapping["file_id"],
            FileLinkCreate(link_type="task", target_id=self.task.id),
        )
        context = AgentRunner()._read_context(
            {"id": "run", "task_id": self.task.id, "project_id": self.project.id}
        )
        self.assertIn("app/main.py", context)
        self.assertIn("def greeting", context)

        restarted = TestClient(create_app())
        self.assertEqual(restarted.get(f"/api/repos/{repo['id']}").status_code, 200)
        self.assertEqual(restarted.get(f"/api/repos/{repo['id']}/files").json()["total"], 2)

    def test_repo_relative_patch_changes_only_managed_copy_and_mapping_hash(self):
        original = (self.source / "app" / "main.py").read_bytes()
        repo = self.register()
        mappings, _ = repo_store.list_repo_files(repo["id"])
        mapping = next(item for item in mappings if item["relative_path"] == "app/main.py")
        proposal = """# Patch Proposal

## Objective
Use an f-string.

## Unified diff
```diff
diff --git a/app/main.py b/app/main.py
--- a/app/main.py
+++ b/app/main.py
@@ -1,2 +1,2 @@
 def greeting(name):
-    return \"Hello \" + name
+    return f\"Hello {name}\"
```

## Notes
This patch has not been applied."""
        artifact = PatchProposalService(generator=lambda _prompt: proposal).propose(
            PatchProposalRequest(
                objective="Use an f-string",
                file_ids=[mapping["file_id"]],
                project_id=self.project.id,
            )
        )
        target = artifact["metadata"]["target_files"][0]
        self.assertEqual(target["repo_id"], repo["id"])
        self.assertEqual(target["relative_path"], "app/main.py")
        service = ControlledPatchApplyService()
        validation = service.validate(artifact["id"], PatchValidateRequest())
        self.assertTrue(validation.valid, validation.errors)
        application, updated = service.apply(artifact["id"], PatchApplyRequest(confirm=True))
        expected = b'def greeting(name):\n    return f"Hello {name}"\n'
        self.assertEqual(
            WorkspaceFilesService().download_path(mapping["file_id"]).read_bytes(), expected
        )
        self.assertEqual((self.source / "app" / "main.py").read_bytes(), original)
        self.assertEqual(updated["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(repo_store.get_repo_file(mapping["id"])["sha256"], updated["sha256"])
        self.assertEqual(application["status"], "applied")

    def test_limits_unsafe_roots_duplicates_and_soft_delete(self):
        missing = self.client.post(
            "/api/repos/register",
            json={"path": str(self.root / "missing"), "confirm": True},
        )
        self.assertEqual(missing.status_code, 400)
        plain_file = self.root / "plain.txt"
        plain_file.write_text("not a repo")
        not_directory = self.client.post(
            "/api/repos/register", json={"path": str(plain_file), "confirm": True}
        )
        self.assertEqual(not_directory.status_code, 400)
        with self.assertRaisesRegex(ValueError, "Root directories"):
            validate_repo_root("/")
        with self.assertRaisesRegex(ValueError, "home directory"):
            validate_repo_root(str(Path.home()))
        with self.assertRaisesRegex(ValueError, "file import cap"):
            scan_repo(
                self.source,
                max_files=1,
                max_total_bytes=10_000,
                max_file_bytes=10_000,
            )
        with self.assertRaisesRegex(ValueError, "per-file cap"):
            scan_repo(
                self.source,
                max_files=10,
                max_total_bytes=10_000,
                max_file_bytes=8,
            )
        with self.assertRaisesRegex(ValueError, "byte import cap"):
            scan_repo(
                self.source,
                max_files=10,
                max_total_bytes=50,
                max_file_bytes=10_000,
            )

        repo = self.register()
        duplicate = self.client.post(
            "/api/repos/register",
            json={"path": str(self.source), "confirm": True},
        )
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(self.client.delete(f"/api/repos/{repo['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/repos/{repo['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/repos").json()["total"], 0)


if __name__ == "__main__":
    unittest.main()
