import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents.runner import AgentRunner
from app.services.agents.store import initialize_agent_tables
from app.services.files import store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate, FileLinkCreate
from app.services.notes import NoteCreate, NotesService
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.store import initialize_task_tables


class FileWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = f"{self.tmp.name}/files"
        os.environ["NEO_WORKSPACE_FILE_MAX_BYTES"] = "64"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        initialize_agent_tables()
        store.initialize_workspace_file_tables()
        self.projects = ProjectsService()
        self.tasks = TasksService()
        self.notes = NotesService()
        self.project = self.projects.create_project(ProjectCreate(title="Files project"))
        self.task = self.tasks.create_task(
            TaskCreate(title="Read code", project_id=self.project.id)
        )
        self.note = self.notes.create_note(NoteCreate(title="File note", body="Context"))
        self.service = WorkspaceFilesService()
        self.client = TestClient(create_app())

    def tearDown(self):
        get_settings.cache_clear()
        for name in ("NEO_DATABASE_URL", "NEO_WORKSPACE_FILES_DIR", "NEO_WORKSPACE_FILE_MAX_BYTES"):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def test_upload_extract_search_read_download_delete_and_persistence(self):
        response = self.client.post(
            "/api/files/upload",
            files={
                "file": (
                    "../app.py",
                    b"def sentinel():\r\n    return 'needle'\r\n",
                    "text/x-python",
                )
            },
            data={"project_id": self.project.id},
        )
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()["file"]
        self.assertEqual(item["filename"], "app.py")
        self.assertEqual(item["extension"], "py")
        self.assertIn("def sentinel", item["extracted_text"])
        self.assertNotIn("storage_path", item)

        by_name = self.client.get("/api/files", params={"q": "app.py"}).json()
        by_text = self.client.get("/api/files", params={"q": "needle"}).json()
        linked = self.client.get("/api/files", params={"project_id": self.project.id}).json()
        self.assertEqual(by_name["total"], 1)
        self.assertEqual(by_text["total"], 1)
        self.assertEqual(linked["total"], 1)

        detail = self.client.get(f"/api/files/{item['id']}").json()
        self.assertEqual(detail["links"][0]["target_id"], self.project.id)
        download = self.client.get(f"/api/files/{item['id']}/download")
        self.assertEqual(download.content, b"def sentinel():\r\n    return 'needle'\r\n")
        self.assertIsNotNone(WorkspaceFilesService().get(item["id"]))

        self.assertEqual(self.client.delete(f"/api/files/{item['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/files/{item['id']}").status_code, 404)
        self.assertEqual(self.client.get("/api/files").json()["total"], 0)

    def test_validation_links_summary_dedup_and_artifacts(self):
        self.assertEqual(
            self.client.post("/api/files/upload", files={"file": ("x.txt", b"")}).status_code, 400
        )
        self.assertEqual(
            self.client.post("/api/files/upload", files={"file": ("x.txt", b"x" * 65)}).status_code,
            400,
        )
        item = self.service.import_bytes(
            original_filename="README.md", content=b"Neo file workspace. Safe preview."
        )

        for kind, target in (
            ("project", self.project.id),
            ("task", self.task.id),
            ("note", self.note.id),
        ):
            first = self.service.attach(
                item["id"], FileLinkCreate(link_type=kind, target_id=target)
            )
            second = self.service.attach(
                item["id"], FileLinkCreate(link_type=kind, target_id=target)
            )
            self.assertEqual(first["id"], second["id"])
            files, total = store.list_files(link_type=kind, target_id=target)
            self.assertEqual(total, 1)
            self.assertEqual(files[0]["id"], item["id"])

        summary = self.service.summarize(item["id"])
        self.assertIn("Safe preview", summary)
        link = store.list_links(item["id"])[0]
        self.assertTrue(store.delete_link(item["id"], link["id"]))

        artifact = self.service.create_artifact(
            ArtifactCreate(
                title="Patch proposal",
                artifact_type="patch_proposal",
                content="# Patch Proposal",
                task_id=self.task.id,
            )
        )
        self.assertEqual(store.get_artifact(artifact["id"])["content"], "# Patch Proposal")
        self.assertEqual(len(store.list_artifacts(task_id=self.task.id)), 1)

    def test_unsupported_binary_stores_metadata_without_preview(self):
        item = self.service.import_bytes(
            original_filename="image.png", content=b"\x89PNG\x00binary"
        )
        self.assertIsNone(item["extracted_text"])
        self.assertFalse(item["metadata"]["preview_supported"])

    def test_agent_context_is_bounded_and_names_files(self):
        item = self.service.import_bytes(
            original_filename="context.py", content=b"IMPORTANT_FILE_SENTINEL = True"
        )
        self.service.attach(item["id"], FileLinkCreate(link_type="task", target_id=self.task.id))
        run = {"id": "run", "task_id": self.task.id, "project_id": self.project.id}
        context = AgentRunner()._read_context(run)
        self.assertIn("context.py", context)
        self.assertIn("IMPORTANT_FILE_SENTINEL", context)
        self.assertIn("Files considered:", context)


if __name__ == "__main__":
    unittest.main()
