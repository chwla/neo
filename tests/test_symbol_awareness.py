import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents.runner import AgentRunner
from app.services.code_index.service import CodeIndexService
from app.services.code_index.types import CodeIndexBuildRequest
from app.services.files import store as file_store
from app.services.patch_apply import (
    ControlledPatchApplyService,
    PatchApplyRequest,
    PatchValidateRequest,
)
from app.services.patches import PatchProposalRequest, PatchProposalService
from app.services.projects import ProjectCreate, ProjectsService
from app.services.repos import store as repo_store
from app.services.symbol_awareness import store as awareness_store
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.symbol_awareness.types import SymbolAwarenessBuildRequest
from app.services.tasks import TaskCreate, TasksService


class SymbolAwarenessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "workspace-files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "workspace-repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(ProjectCreate(title="Symbols project"))
        self.task = TasksService().create_task(
            TaskCreate(
                title="Change createTask behavior",
                description="Update the Tasks component and API helper",
                project_id=self.project.id,
            )
        )
        self.source = self.root / "source"
        (self.source / "backend").mkdir(parents=True)
        (self.source / "frontend").mkdir()
        (self.source / "backend" / "tasks.py").write_text(
            """class TaskService:
    def create_task(self):
        return True

def run_task():
    service = TaskService()
    return service.create_task()
"""
        )
        (self.source / "backend" / "routes.py").write_text(
            """from fastapi import APIRouter
from backend.tasks import TaskService

router = APIRouter()

@router.post("/tasks")
def create_task():
    return TaskService().create_task()
"""
        )
        (self.source / "frontend" / "api.ts").write_text(
            "export async function createTask() { return true; }\n"
        )
        (self.source / "frontend" / "Tasks.tsx").write_text(
            """import React from "react";
import { createTask } from "./api";
export default function Tasks() { return <button>Tasks</button>; }
const saveTask = async () => createTask();
"""
        )
        (self.source / "frontend" / "App.tsx").write_text(
            """import Tasks from "./Tasks";
export default function App() { return <Tasks />; }
"""
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
        self.index = CodeIndexService()
        self.awareness = SymbolAwarenessService()

    def tearDown(self):
        get_settings.cache_clear()
        for name in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
        ):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def mapping(self, path):
        mappings, _ = repo_store.list_repo_files(self.repo["id"])
        return next(item for item in mappings if item["relative_path"] == path)

    def build_index(self, force=False):
        return self.index.build(self.repo["id"], CodeIndexBuildRequest(force=force, summarize=True))

    def build_awareness(self, force=False):
        return self.awareness.build(self.repo["id"], SymbolAwarenessBuildRequest(force=force))

    def test_requires_index_and_rejects_missing_deleted_repo(self):
        response = self.client.post(
            f"/api/symbols/repos/{self.repo['id']}/build", json={"force": False}
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(
            self.client.post("/api/symbols/repos/missing/build", json={"force": True}).status_code,
            404,
        )
        self.client.delete(f"/api/repos/{self.repo['id']}")
        self.assertEqual(
            self.client.post(
                f"/api/symbols/repos/{self.repo['id']}/build", json={"force": True}
            ).status_code,
            404,
        )

    def test_build_references_relationships_related_files_and_navigation(self):
        self.build_index()
        result = self.build_awareness()
        self.assertEqual(result["status"], "ready", result.get("errors"))
        self.assertGreater(result["stats"]["reference_count"], 8)
        self.assertGreater(result["stats"]["resolved_reference_count"], 3)
        self.assertGreater(result["stats"]["relationship_count"], 1)
        self.assertGreater(result["stats"]["related_file_count"], 2)

        definitions = self.awareness.definitions(self.repo["id"], "createTask")
        self.assertEqual(definitions[0]["relative_path"], "frontend/api.ts")
        self.assertNotIn("export", {item["symbol_type"] for item in definitions})
        contextual = self.awareness.definitions(
            self.repo["id"], "create_task", "backend/routes.py", 7
        )
        self.assertEqual(contextual[0]["relative_path"], "backend/routes.py")

        symbol_id = definitions[0]["symbol_id"]
        references, total = self.awareness.references_for_symbol(symbol_id)
        self.assertGreaterEqual(total, 2)
        self.assertTrue(
            any(item["source_relative_path"] == "frontend/Tasks.tsx" for item in references)
        )
        named, _ = self.awareness.references_by_name(self.repo["id"], "Tasks")
        self.assertTrue(any(item["reference_type"] == "component_usage" for item in named))
        unresolved, _ = self.awareness.references_by_name(self.repo["id"], "React")
        self.assertTrue(any(not item["resolved"] for item in unresolved))

        tasks_mapping = self.mapping("frontend/Tasks.tsx")
        document = self.awareness.document_symbols(self.repo["id"], tasks_mapping["id"])
        self.assertIn("Tasks", {item["name"] for item in document})
        related = self.awareness.related_files(self.repo["id"], tasks_mapping["id"])
        self.assertIn("frontend/api.ts", {item["target_relative_path"] for item in related})
        context = self.awareness.symbol_context(symbol_id)
        self.assertIn("createTask", context["definition_excerpt"])
        self.assertTrue(context["references"])

        relationships = awareness_store.list_relationships(self.repo["id"])
        self.assertIn("defines_route", {item["relationship_type"] for item in relationships})
        self.assertIn("calls", {item["relationship_type"] for item in relationships})
        restarted = TestClient(create_app())
        persisted = restarted.get(f"/api/symbols/repos/{self.repo['id']}")
        self.assertEqual(persisted.status_code, 200)
        self.assertEqual(persisted.json()["status"], "ready")

    def test_endpoints_rebuild_clears_old_references_and_partial_failure(self):
        self.build_index()
        response = self.client.post(
            f"/api/symbols/repos/{self.repo['id']}/build", json={"force": False}
        )
        self.assertEqual(response.status_code, 200, response.text)
        definitions = self.client.get(
            f"/api/symbols/repos/{self.repo['id']}/definition",
            params={"name": "createTask"},
        ).json()["definitions"]
        symbol_id = definitions[0]["symbol_id"]
        self.assertEqual(self.client.get(f"/api/symbols/{symbol_id}/references").status_code, 200)
        self.assertEqual(
            self.client.get(
                f"/api/symbols/repos/{self.repo['id']}/references",
                params={"name": "createTask"},
            ).status_code,
            200,
        )
        mapping = self.mapping("frontend/Tasks.tsx")
        self.assertEqual(
            self.client.get(
                f"/api/symbols/repos/{self.repo['id']}/files/{mapping['id']}/document-symbols"
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/symbols/repos/{self.repo['id']}/files/{mapping['id']}/related-files"
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(f"/api/symbols/{symbol_id}/context").status_code, 200)

        old_ids = {
            item["id"]
            for item in awareness_store.list_references(repo_id=self.repo["id"], limit=500)[0]
        }
        self.build_awareness(force=True)
        new_ids = {
            item["id"]
            for item in awareness_store.list_references(repo_id=self.repo["id"], limit=500)[0]
        }
        self.assertTrue(old_ids.isdisjoint(new_ids))

        broken_mapping = self.mapping("backend/tasks.py")
        file_store.update_file(broken_mapping["file_id"], {"extracted_text": "def broken(:\n"})
        partial = self.build_awareness(force=True)
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(partial["errors"])

    def test_chat_agent_and_patch_targeting_use_symbol_awareness(self):
        self.build_index()
        self.build_awareness()
        chat_context = self.awareness.context_for_prompt(
            "Where is createTask defined and what uses createTask?"
        )
        self.assertIn("frontend/api.ts", chat_context)
        self.assertIn("frontend/Tasks.tsx", chat_context)

        agent_context = AgentRunner()._read_context(
            {
                "id": "run",
                "task_id": self.task.id,
                "project_id": self.project.id,
                "objective": "Change createTask behavior",
            }
        )
        self.assertIn("Symbol awareness used:", agent_context)
        self.assertIn("Symbol-related files considered:", agent_context)

        proposal = """# Patch Proposal
## Unified diff
```diff
diff --git a/frontend/api.ts b/frontend/api.ts
--- a/frontend/api.ts
+++ b/frontend/api.ts
@@ -1 +1 @@
-export async function createTask() { return true; }
+export async function createTask() { return false; }
```
## Notes
This patch has not been applied."""
        artifact = PatchProposalService(generator=lambda _prompt: proposal).propose(
            PatchProposalRequest(objective="Change createTask behavior", project_id=self.project.id)
        )
        targets = {item.get("relative_path") for item in artifact["metadata"]["target_files"]}
        self.assertIn("frontend/api.ts", targets)
        self.assertIn("frontend/Tasks.tsx", targets)

    def test_code_index_rebuild_invalidates_awareness_for_safe_rebuild(self):
        self.build_index()
        self.build_awareness()
        original = (self.source / "frontend" / "Tasks.tsx").read_text()
        mapping = self.mapping("frontend/Tasks.tsx")
        proposal = """# Patch Proposal
## Unified diff
```diff
diff --git a/frontend/Tasks.tsx b/frontend/Tasks.tsx
--- a/frontend/Tasks.tsx
+++ b/frontend/Tasks.tsx
@@ -4 +4 @@
-const saveTask = async () => createTask();
+const saveTask = async () => createTask({ due: true });
```
## Notes
This patch has not been applied."""
        artifact = PatchProposalService(generator=lambda _prompt: proposal).propose(
            PatchProposalRequest(objective="Pass a due date", file_ids=[mapping["file_id"]])
        )
        patch_service = ControlledPatchApplyService()
        self.assertTrue(patch_service.validate(artifact["id"], PatchValidateRequest()).valid)
        patch_service.apply(artifact["id"], PatchApplyRequest(confirm=True))
        self.build_index(force=True)
        self.assertEqual(self.awareness.status(self.repo["id"])["status"], "not_built")
        rebuilt = self.build_awareness(force=True)
        self.assertEqual(rebuilt["status"], "ready")
        references, _ = self.awareness.references_by_name(self.repo["id"], "createTask")
        self.assertTrue(any("due" in (item["context_text"] or "") for item in references))
        self.assertEqual((self.source / "frontend" / "Tasks.tsx").read_text(), original)


if __name__ == "__main__":
    unittest.main()
