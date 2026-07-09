import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agents.service import AgentsService
from app.services.agents.types import AgentRunCreate
from app.services.repos.store import get_repo
from app.services.research.jobs import create_job
from app.services.tasks import TaskCreate, TasksService


class RulesSystemTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())

    def tearDown(self):
        get_settings.cache_clear()
        for key in ("NEO_DATABASE_URL", "NEO_WORKSPACE_FILES_DIR", "NEO_WORKSPACE_REPOS_DIR"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def create_profile(self, scope_type, scope_id, name, rules, priority=100):
        response = self.client.post(
            "/api/rules/profiles",
            json={
                "scope_type": scope_type,
                "scope_id": scope_id,
                "name": name,
                "priority": priority,
                "rules": rules,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["profile"]

    def test_crud_precedence_merge_safety_and_logs(self):
        workspace = self.create_profile(
            "workspace",
            None,
            "Workspace",
            {
                "instructions": ["Small diffs", "Shared"],
                "forbidden_paths": ["generated"],
                "approval_defaults": {"require_patch_approval": False},
                "patch_constraints": {"max_files": 6},
            },
        )
        self.create_profile(
            "repo",
            "repo-1",
            "Repo",
            {
                "instructions": ["Shared", "Repo patterns"],
                "forbidden_paths": ["migrations"],
                "checkpoint_template": "Task: {task_title}",
                "patch_constraints": {"max_files": 3, "allow_new_files": False},
                "unknown_option": {"preserved": True},
            },
        )
        response = self.client.post(
            "/api/rules/resolve",
            json={"context_type": "coding_agent", "repo_id": "repo-1"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        rules = result["resolved_rules"]
        self.assertEqual(rules["instructions"], ["Small diffs", "Shared", "Repo patterns"])
        self.assertTrue(
            {"generated", "migrations", ".env", ".git"} <= set(rules["forbidden_paths"])
        )
        self.assertTrue(rules["approval_defaults"]["require_patch_approval"])
        self.assertEqual(rules["patch_constraints"]["max_files"], 3)
        self.assertFalse(rules["patch_constraints"]["allow_new_files"])
        self.assertTrue(any("Safety override ignored" in item for item in result["warnings"]))
        self.assertEqual(rules["metadata"]["unknown_fields"]["unknown_option"], {"preserved": True})
        logs = self.client.get("/api/rules/resolution-logs").json()
        self.assertEqual(logs["total"], 1)

        disabled = self.client.delete(f"/api/rules/profiles/{workspace['id']}")
        self.assertFalse(disabled.json()["profile"]["enabled"])
        self.assertEqual(self.client.get(f"/api/rules/profiles/{workspace['id']}").status_code, 200)

    def test_imports_markdown_json_and_invalid_json(self):
        source = self.root / "source"
        (source / ".neo").mkdir(parents=True)
        (source / "main.py").write_text("print('ok')\n")
        (source / "AGENTS.md").write_text("Prefer small diffs.")
        (source / ".neo" / "rules.json").write_text(
            '{"test_preferences":[{"name":"Tests","command_hint":["python","-m","pytest","-q"]}]}'
        )
        registered = self.client.post(
            "/api/repos/register", json={"path": str(source), "confirm": True}
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        repo_id = registered.json()["repo"]["id"]
        imported = self.client.post(f"/api/rules/repos/{repo_id}/import")
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(len(imported.json()["profiles"]), 2)

        managed = Path(get_repo(repo_id)["workspace_path"])
        (managed / ".neo" / "rules.json").write_text("{")
        invalid = self.client.post(f"/api/rules/repos/{repo_id}/import").json()
        json_profile = next(
            item for item in invalid["profiles"] if item["source_path"].endswith("json")
        )
        self.assertFalse(json_profile["enabled"])
        self.assertTrue(invalid["warnings"])

    def test_profile_edit_preserves_unknown_fields_and_can_reenable(self):
        profile = self.create_profile(
            "workspace",
            None,
            "Before",
            {"instructions": ["Before"], "extension_data": {"keep": True}},
        )
        updated = self.client.patch(
            f"/api/rules/profiles/{profile['id']}",
            json={
                "name": "After",
                "description": "Edited",
                "priority": 42,
                "enabled": False,
                "rules": {
                    "instructions": ["After"],
                    "extension_data": {"keep": True},
                },
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        item = updated.json()["profile"]
        self.assertEqual(item["name"], "After")
        self.assertFalse(item["enabled"])
        self.assertEqual(item["rules"]["extension_data"], {"keep": True})
        enabled = self.client.patch(f"/api/rules/profiles/{profile['id']}", json={"enabled": True})
        self.assertTrue(enabled.json()["profile"]["enabled"])

    def test_chat_research_and_agent_store_resolved_rule_context(self):
        self.create_profile(
            "workspace",
            None,
            "Consumer rules",
            {
                "instructions": ["Use concise evidence."],
                "coding_style": ["Follow local patterns."],
                "research_preferences": ["Prefer primary sources."],
                "test_preferences": [
                    {
                        "name": "Backend",
                        "command_hint": ["python", "-m", "pytest", "-q"],
                    }
                ],
            },
        )
        chat = self.client.post("/api/chats", json={}).json()
        answer = self.client.post(
            f"/api/chats/{chat['id']}/messages",
            json={"prompt": "Which rules are active here?"},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIn("Consumer rules", answer.json()["reply"])
        self.assertIn("Use concise evidence.", answer.json()["reply"])

        research = create_job("Compare options")
        self.assertEqual(
            research.metadata["resolved_rules"]["research_preferences"],
            ["Prefer primary sources."],
        )

        class PassiveRunner:
            def start(self, _run_id):
                return None

        task = TasksService().create_task(TaskCreate(title="Rule-aware agent"))
        run = AgentsService(runner=PassiveRunner()).create_run(AgentRunCreate(task_id=task.id))
        detail = AgentsService(runner=PassiveRunner()).read_run(run.id)
        rule_snapshot = detail[1][0].input["rules"]
        self.assertIn("Follow local patterns.", rule_snapshot["resolved_rules"]["coding_style"])
        self.assertEqual(
            rule_snapshot["resolved_rules"]["test_preferences"][0]["command_hint"],
            ["python", "-m", "pytest", "-q"],
        )
        logs = self.client.get("/api/rules/resolution-logs?limit=100").json()
        contexts = {item["context_type"] for item in logs["resolution_logs"]}
        self.assertTrue({"chat", "research", "agent"} <= contexts)

    def test_provider_route_overrides_validate_and_fallback_with_warning(self):
        valid = self.create_profile(
            "workspace",
            None,
            "Routes",
            {
                "model_routes": {
                    "chat": "chat",
                    "research": "research",
                    "agent": "agent",
                    "coding_agent": "coding_agent",
                    "patch_proposal": "patch_proposal",
                }
            },
        )
        resolved = self.client.post("/api/rules/resolve", json={"context_type": "chat"}).json()
        self.assertEqual(resolved["resolved_rules"]["model_routes"]["chat"], "chat")
        self.assertFalse(resolved["warnings"])

        self.client.patch(
            f"/api/rules/profiles/{valid['id']}",
            json={"rules": {"model_routes": {"chat": "missing-route"}}},
        )
        invalid = self.client.post("/api/rules/resolve", json={"context_type": "chat"}).json()
        self.assertNotIn("chat", invalid["resolved_rules"]["model_routes"])
        self.assertTrue(any("normal routing" in warning for warning in invalid["warnings"]))

        self.client.patch(
            f"/api/rules/profiles/{valid['id']}",
            json={"rules": {"model_routes": {"research": "research"}}},
        )
        disabled = self.client.patch("/api/llm/routes/research", json={"enabled": False})
        self.assertEqual(disabled.status_code, 200, disabled.text)
        disabled_result = self.client.post(
            "/api/rules/resolve", json={"context_type": "research"}
        ).json()
        self.assertNotIn("research", disabled_result["resolved_rules"]["model_routes"])
        self.assertTrue(any("disabled" in warning for warning in disabled_result["warnings"]))


if __name__ == "__main__":
    unittest.main()
