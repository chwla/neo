import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.agent_framework import AgentDefinitionService
from app.services.agents import store as agent_store
from app.services.tasks import TaskCreate, TasksService


class AgentFrameworkTest(unittest.TestCase):
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

    def test_builtins_seed_idempotently_and_api_lists_roles(self):
        first = self.client.get("/api/agents/definitions")
        self.assertEqual(first.status_code, 200, first.text)
        names = {item["name"] for item in first.json()["definitions"]}
        self.assertTrue(
            {
                "general",
                "planner",
                "coder",
                "reviewer",
                "tester",
                "researcher",
                "refactor",
                "explorer",
                "summarizer",
            }
            <= names
        )
        reset = self.client.post("/api/agents/definitions/reset-builtins")
        self.assertEqual(reset.status_code, 200, reset.text)
        second = self.client.get("/api/agents/definitions").json()["definitions"]
        self.assertEqual(len(names), len({item["name"] for item in second if item["built_in"]}))

    def test_custom_agent_persists_route_and_clamps_unsafe_permissions(self):
        payload = {
            "name": "My Reviewer",
            "display_name": "My Reviewer",
            "description": "Review only",
            "agent_type": "reviewer",
            "system_prompt": "Review the diff and risks.",
            "default_route_name": "chat",
            "permissions": {
                "can_propose_patch": True,
                "can_request_tests": True,
                "can_request_checkpoint": True,
                "can_delegate": True,
                "max_delegations": 3,
            },
            "tools": ["read_files", "shell"],
        }
        created = self.client.post("/api/agents/definitions", json=payload)
        self.assertEqual(created.status_code, 201, created.text)
        agent = created.json()["definition"]
        self.assertEqual(agent["default_route_name"], "chat")
        self.assertFalse(agent["permissions"]["can_propose_patch"])
        self.assertFalse(agent["permissions"]["can_request_tests"])
        self.assertFalse(agent["permissions"]["can_request_checkpoint"])
        self.assertEqual(agent["tools"], ["read_files"])
        self.assertTrue(
            any("Unsafe permission ignored" in item for item in agent["safety_warnings"])
        )
        self.assertTrue(any("shell" in item for item in agent["safety_warnings"]))

        service = AgentDefinitionService()
        persisted = service.get(agent["id"])
        self.assertEqual(persisted.name, agent["name"])
        self.assertEqual(persisted.default_route_name, "chat")

    def test_disabled_agent_cannot_be_selected(self):
        created = self.client.post(
            "/api/agents/definitions",
            json={
                "name": "disabled-coder",
                "agent_type": "coder",
                "system_prompt": "Propose patches.",
                "permissions": {"can_propose_patch": True},
            },
        ).json()["definition"]
        self.client.delete(f"/api/agents/definitions/{created['id']}")
        service = AgentDefinitionService()
        fallback = service.resolve_for_run(created["id"], fallback="general")
        self.assertEqual(fallback.name, "general")

    def test_agent_bound_rule_profile_is_resolved(self):
        profile = self.client.post(
            "/api/rules/profiles",
            json={
                "scope_type": "workspace",
                "scope_id": None,
                "name": "Agent profile",
                "rules": {"instructions": ["Agent profile instruction"]},
            },
        ).json()["profile"]
        agent = self.client.post(
            "/api/agents/definitions",
            json={
                "name": "profiled-agent",
                "agent_type": "custom",
                "system_prompt": "Use profile.",
                "rules_profile_ids": [profile["id"]],
                "permissions": {},
            },
        ).json()["definition"]
        task = TasksService().create_task(TaskCreate(title="Profiled task"))
        response = self.client.post(
            "/api/agents/runs",
            json={"task_id": task.id, "objective": "Use rules", "agent_definition_id": agent["id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run"]["id"]
        detail = self.client.get(f"/api/agents/runs/{run_id}").json()
        rules = detail["steps"][0]["input"]["rules"]
        self.assertIn("Agent profile instruction", rules["resolved_rules"]["instructions"])
        self.assertEqual(detail["run"]["agent_definition_id"], agent["id"])

    def test_delegation_requires_permission_and_enforces_limits(self):
        task = TasksService().create_task(TaskCreate(title="Delegation task"))
        now = agent_store.now_iso()
        run = agent_store.insert_run(
            {
                "id": "parent-run",
                "task_id": task.id,
                "project_id": None,
                "title": "Parent",
                "objective": "Delegate",
                "status": "queued",
                "mode": "assist",
                "plan": [],
                "final_output": None,
                "error": None,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "cancelled_at": None,
                "forked_from_run_id": None,
                "agent_definition_id": "builtin-general",
            }
        )
        reviewer_denied = self.client.post(
            "/api/agents/delegations",
            json={
                "parent_run_id": run["id"],
                "parent_agent_id": "reviewer",
                "child_agent_id": "coder",
                "objective": "Try delegation",
            },
        )
        self.assertEqual(reviewer_denied.status_code, 400)

        first = self.client.post(
            "/api/agents/delegations",
            json={
                "parent_run_id": run["id"],
                "parent_agent_id": "general",
                "child_agent_id": "planner",
                "objective": "Plan this safely",
            },
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["delegation"]["parent_run_id"], run["id"])
        second = self.client.post(
            "/api/agents/delegations",
            json={
                "parent_run_id": run["id"],
                "parent_agent_id": "general",
                "child_agent_id": "summarizer",
                "objective": "Summarize this safely",
            },
        )
        self.assertEqual(second.status_code, 201, second.text)
        limited = self.client.post(
            "/api/agents/delegations",
            json={
                "parent_run_id": run["id"],
                "parent_agent_id": "general",
                "child_agent_id": "tester",
                "objective": "Third child should exceed general default",
            },
        )
        self.assertEqual(limited.status_code, 400)

    def test_coding_run_records_role_agents_without_approval_bypass(self):
        from app.services.coding_agent.types import CodingRunCreate
        from tests.test_coding_agent import MultiStepCodingAgentTest

        case = MultiStepCodingAgentTest(
            methodName="test_start_selects_indexed_symbol_context_and_waits_without_applying"
        )
        case.setUp()
        try:
            detail = case.orchestrator().start(
                CodingRunCreate(
                    objective="Add the safe change",
                    task_id=case.task.id,
                    project_id=case.project.id,
                    repo_id=case.repo["id"],
                    agent_definition_id="coder",
                )
            )
            self.assertEqual(detail["coding_run"]["agent_definition_id"], "builtin-coder")
            self.assertIn("planner", detail["role_agents"])
            self.assertIn("coder", detail["role_agents"])
            self.assertEqual(detail["current_action_request"]["action_type"], "apply_patch")
            self.assertEqual(detail["current_action_request"]["status"], "pending")
            step_titles = [step["title"] for step in detail["steps"]]
            self.assertTrue(any("Coder: create patch proposal" in title for title in step_titles))
        finally:
            case.tearDown()
