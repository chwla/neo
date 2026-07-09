import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents.runner import AgentRunner
from app.services.code_index import store as index_store
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
from app.services.tasks import TaskCreate, TasksService


class CodebaseIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "workspace-files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "workspace-repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.project = ProjectsService().create_project(ProjectCreate(title="Index project"))
        self.task = TasksService().create_task(
            TaskCreate(
                title="Update task creation",
                description="Change create_task behavior",
                project_id=self.project.id,
            )
        )
        self.source = self.root / "source"
        (self.source / "app" / "api").mkdir(parents=True)
        (self.source / "frontend").mkdir()
        (self.source / "db").mkdir()
        (self.source / "native").mkdir()
        (self.source / "app" / "api" / "tasks.py").write_text(
            """from fastapi import APIRouter
from app.services.tasks import service

router = APIRouter()
MAX_TASKS = 100

class TaskRequest:
    \"\"\"Task creation input.\"\"\"
    pass

@router.post("/tasks")
async def create_task():
    return {"ok": True}
"""
        )
        (self.source / "frontend" / "api.ts").write_text(
            "export async function startTask() { return true; }\n"
        )
        (self.source / "frontend" / "Tasks.tsx").write_text(
            """import React from "react";
import { startTask } from "./api";
export default function Tasks() { return <button>Tasks</button>; }
const fetchTasks = async () => startTask();
"""
        )
        (self.source / "native" / "util.c").write_text(
            "#include <stdio.h>\nint calculate(int x) { return x + 1; }\n"
        )
        (self.source / "db" / "schema.sql").write_text(
            "CREATE TABLE tasks(id INTEGER);\nCREATE INDEX idx_tasks ON tasks(id);\n"
        )
        (self.source / "README.md").write_text("# Index Fixture\n## Architecture\n")
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
        self.service = CodeIndexService()

    def tearDown(self):
        get_settings.cache_clear()
        for name in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
        ):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def build(self, force=False):
        return self.service.build(
            self.repo["id"], CodeIndexBuildRequest(force=force, summarize=True)
        )

    def mapping(self, path):
        mappings, _ = repo_store.list_repo_files(self.repo["id"])
        return next(item for item in mappings if item["relative_path"] == path)

    def test_build_extract_search_routes_dependencies_summaries_and_persistence(self):
        item = self.build()
        self.assertEqual(item["status"], "ready", item["metadata"])
        self.assertEqual(item["indexed_file_count"], 6)
        self.assertGreaterEqual(item["symbol_count"], 10)
        self.assertEqual(item["route_count"], 1)

        symbols, _ = index_store.list_symbols(self.repo["id"], q="create_task")
        self.assertIn("async_function", {symbol["symbol_type"] for symbol in symbols})
        classes, _ = index_store.list_symbols(self.repo["id"], symbol_type="class")
        self.assertIn("TaskRequest", {symbol["name"] for symbol in classes})
        components, _ = index_store.list_symbols(self.repo["id"], symbol_type="component")
        self.assertIn("Tasks", {symbol["name"] for symbol in components})
        exports, _ = index_store.list_symbols(self.repo["id"], symbol_type="export")
        self.assertIn("startTask", {symbol["name"] for symbol in exports})
        tables, _ = index_store.list_symbols(self.repo["id"], symbol_type="table")
        self.assertEqual(tables[0]["name"], "tasks")
        headings, _ = index_store.list_symbols(self.repo["id"], symbol_type="heading")
        self.assertIn("Architecture", {symbol["name"] for symbol in headings})

        routes = self.service.routes(self.repo["id"])
        self.assertEqual(routes[0]["method"], "POST")
        self.assertEqual(routes[0]["handler"], "create_task")
        dependencies = index_store.list_dependencies(self.repo["id"])
        internal = next(item for item in dependencies if '"./api"' in item["import_text"])
        external = next(item for item in dependencies if '"react"' in item["import_text"])
        self.assertTrue(internal["resolved"])
        self.assertEqual(internal["target_relative_path"], "frontend/api.ts")
        self.assertFalse(external["resolved"])
        self.assertEqual(external["dependency_type"], "external")

        search = self.service.search(self.repo["id"], "task creation", 20)
        self.assertTrue(any(item["relative_path"] == "app/api/tasks.py" for item in search))
        chat_context = self.service.context_for_prompt(
            "Where is create_task implemented in this repo?"
        )
        self.assertIn("app/api/tasks.py", chat_context)
        self.assertIn("create_task", chat_context)
        summary = self.service.file_summary(self.repo["id"], self.mapping("app/api/tasks.py")["id"])
        self.assertIn("API route", summary["summary"]["purpose"])
        self.assertIn("create_task", summary["summary"]["key_symbols"])

        restarted = TestClient(create_app())
        response = restarted.get(f"/api/code-index/repos/{self.repo['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stats"]["route_count"], 1)

    def test_endpoints_filters_symbol_detail_and_rebuild_clears_stale_entries(self):
        build = self.client.post(
            f"/api/code-index/repos/{self.repo['id']}/build",
            json={"force": False, "summarize": True},
        )
        self.assertEqual(build.status_code, 200, build.text)
        symbol_data = self.client.get(
            f"/api/code-index/repos/{self.repo['id']}/symbols",
            params={"symbol_type": "component"},
        ).json()
        self.assertEqual(symbol_data["symbols"][0]["name"], "Tasks")
        symbol_id = symbol_data["symbols"][0]["id"]
        self.assertEqual(self.client.get(f"/api/code-index/symbols/{symbol_id}").status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/code-index/repos/{self.repo['id']}/routes").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/code-index/repos/{self.repo['id']}/dependencies").status_code,
            200,
        )
        self.assertTrue(
            self.client.get(
                f"/api/code-index/repos/{self.repo['id']}/search",
                params={"q": "Tasks component"},
            ).json()["results"]
        )

        before_ids = {
            item["id"] for item in index_store.list_symbols(self.repo["id"], limit=500)[0]
        }
        rebuilt = self.build(force=True)
        after_ids = {item["id"] for item in index_store.list_symbols(self.repo["id"], limit=500)[0]}
        self.assertEqual(rebuilt["status"], "ready")
        self.assertTrue(before_ids.isdisjoint(after_ids))

    def test_partial_failure_agent_context_and_index_selected_patch(self):
        self.build()
        context, considered = self.service.context_for_project(
            self.project.id, "change create_task behavior"
        )
        self.assertIn("app/api/tasks.py", context)
        self.assertIn("app/api/tasks.py", considered)
        run_context = AgentRunner()._read_context(
            {
                "id": "run",
                "task_id": self.task.id,
                "project_id": self.project.id,
                "objective": "change create_task behavior",
            }
        )
        self.assertIn("Codebase index used:", run_context)
        self.assertIn("Index target files considered:", run_context)

        proposal_text = """# Patch Proposal
## Unified diff
```diff
diff --git a/app/api/tasks.py b/app/api/tasks.py
--- a/app/api/tasks.py
+++ b/app/api/tasks.py
@@ -12,2 +12,2 @@
 async def create_task():
-    return {"ok": True}
+    return {"created": True}
```
## Notes
This patch has not been applied."""
        artifact = PatchProposalService(generator=lambda _prompt: proposal_text).propose(
            PatchProposalRequest(
                objective="change create_task behavior", project_id=self.project.id
            )
        )
        target = artifact["metadata"]["target_files"][0]
        self.assertEqual(target["relative_path"], "app/api/tasks.py")

        mapping = self.mapping("app/api/tasks.py")
        broken = "def broken(:\n"
        file_store.update_file(
            mapping["file_id"],
            {"extracted_text": broken, "sha256": "stale-for-index-test"},
        )
        partial = self.build(force=True)
        self.assertEqual(partial["status"], "partial")
        self.assertTrue(partial["metadata"]["errors"])

    def test_patch_apply_then_rebuild_reflects_new_symbol_and_original_is_untouched(self):
        self.build()
        mapping = self.mapping("frontend/api.ts")
        original = (self.source / "frontend" / "api.ts").read_text()
        proposal = """# Patch Proposal
## Unified diff
```diff
diff --git a/frontend/api.ts b/frontend/api.ts
--- a/frontend/api.ts
+++ b/frontend/api.ts
@@ -1 +1 @@
-export async function startTask() { return true; }
+export async function launchTask() { return true; }
```
## Notes
This patch has not been applied."""
        artifact = PatchProposalService(generator=lambda _prompt: proposal).propose(
            PatchProposalRequest(
                objective="rename startTask to launchTask", file_ids=[mapping["file_id"]]
            )
        )
        apply_service = ControlledPatchApplyService()
        self.assertTrue(apply_service.validate(artifact["id"], PatchValidateRequest()).valid)
        apply_service.apply(artifact["id"], PatchApplyRequest(confirm=True))
        self.assertEqual((self.source / "frontend" / "api.ts").read_text(), original)
        rebuilt = self.build(force=True)
        self.assertEqual(rebuilt["status"], "ready")
        symbols, _ = index_store.list_symbols(self.repo["id"], q="launchTask")
        self.assertEqual(symbols[0]["name"], "launchTask")
        summary = index_store.get_summary(mapping["id"])
        self.assertEqual(
            summary["metadata"]["source_sha256"],
            repo_store.get_repo_file(mapping["id"])["sha256"],
        )

    def test_deleted_repo_cannot_be_indexed(self):
        self.client.delete(f"/api/repos/{self.repo['id']}")
        response = self.client.post(
            f"/api/code-index/repos/{self.repo['id']}/build",
            json={"force": True, "summarize": True},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
