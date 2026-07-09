import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.tools.executor import ToolsService


class ToolsFrameworkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "repos")
        get_settings.cache_clear()
        self.client = TestClient(create_app())
        self.agent_count = 0

    def tearDown(self):
        get_settings.cache_clear()
        for key in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
            "NEO_SECRET_VALUE",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _agent(self, tools=None, skills=None):
        self.agent_count += 1
        response = self.client.post(
            "/api/agents/definitions",
            json={
                "name": f"tool-agent-{self.agent_count}",
                "agent_type": "custom",
                "system_prompt": "Use tools only through the audited framework.",
                "tools": tools or [],
                "skills": skills or [],
                "permissions": {},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["definition"]

    def _server(self, **overrides):
        payload = {
            "name": "Mock MCP",
            "server_type": "stdio",
            "command_json": ["python", "--version"],
            "env_json": {},
            "enabled": True,
            "approval_required": True,
            "metadata": {},
            **overrides,
        }
        response = self.client.post("/api/tools/servers", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["server"]

    def _tool(self, server_id=None, **overrides):
        payload = {
            "server_id": server_id,
            "name": "mock_search",
            "display_name": "Mock search",
            "description": "Read-only mocked MCP search.",
            "category": "external_read",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 100}},
                "required": ["query"],
                "additionalProperties": False,
            },
            "output_schema": {},
            "permissions": {},
            "enabled": True,
            "metadata": {"executor": "mcp.read_only"},
            **overrides,
        }
        response = self.client.post("/api/tools/definitions", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["definition"]

    def test_server_tool_skill_crud_and_persistence(self):
        server = self._server()
        tool = self._tool(server["id"])
        skill_response = self.client.post(
            "/api/tools/skills",
            json={
                "name": "mock_skill",
                "instructions": "Use the mock tool safely.",
                "tool_ids": [tool["id"]],
            },
        )
        self.assertEqual(skill_response.status_code, 201, skill_response.text)
        skill = skill_response.json()["skill"]
        self.client.patch(f"/api/tools/servers/{server['id']}", json={"name": "Mock MCP Edited"})
        self.client.patch(
            f"/api/tools/definitions/{tool['id']}",
            json={"display_name": "Mock Search Edited"},
        )
        self.client.patch(
            f"/api/tools/skills/{skill['id']}",
            json={"display_name": "Mock Skill Edited"},
        )

        fresh = ToolsService()
        self.assertTrue(any(item.id == server["id"] for item in fresh.list_servers()))
        self.assertTrue(any(item.id == tool["id"] for item in fresh.list_tools()))
        self.assertTrue(any(item.id == skill["id"] for item in fresh.list_skills()))

    def test_read_only_auto_call_logs_audit_and_validates_input(self):
        agent = self._agent(tools=["builtin.summarize_text"])
        ok = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": "builtin.summarize_text",
                "agent_definition_id": agent["id"],
                "input": {"text": "one two three four five", "limit": 1},
            },
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        call = ok.json()["call"]
        self.assertEqual(call["status"], "completed")
        self.assertEqual(call["approval_status"], "not_required")
        self.assertIn("summary", call["output"])

        bad = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": "builtin.summarize_text",
                "agent_definition_id": agent["id"],
                "input": {"text": "ok", "unexpected": True},
            },
        )
        self.assertEqual(bad.status_code, 201, bad.text)
        self.assertEqual(bad.json()["call"]["status"], "blocked")
        history = self.client.get("/api/tools/calls").json()["calls"]
        self.assertGreaterEqual(len(history), 2)

    def test_mutating_tool_requires_approval_and_reject_does_not_execute(self):
        agent = self._agent(tools=["builtin.create_note"])
        pending = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": "builtin.create_note",
                "agent_definition_id": agent["id"],
                "input": {"title": "Approval gate", "body": "Do not auto-write."},
            },
        ).json()["call"]
        self.assertEqual(pending["status"], "pending_approval")
        self.assertEqual(pending["approval_status"], "pending")

        rejected = self.client.post(
            f"/api/tools/calls/{pending['id']}/reject",
            json={"reason": "No thanks."},
        ).json()["call"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(rejected["output"])

    def test_approve_executes_after_pending_gate(self):
        agent = self._agent(tools=["builtin.create_note"])
        pending = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": "builtin.create_note",
                "agent_definition_id": agent["id"],
                "input": {"title": "Approved note"},
            },
        ).json()["call"]
        approved = self.client.post(f"/api/tools/calls/{pending['id']}/approve").json()["call"]
        self.assertEqual(approved["status"], "completed")
        self.assertEqual(approved["approval_status"], "approved")
        self.assertFalse(approved["output"]["created"])

    def test_disabled_server_tool_agent_permission_and_skill_restriction_block_calls(self):
        server = self._server()
        tool = self._tool(server["id"])
        other_tool = self._tool(server["id"], name="other_search")
        agent = self._agent(tools=[tool["id"]])
        skill = self.client.post(
            "/api/tools/skills",
            json={
                "name": "restricted_skill",
                "instructions": "Only one tool.",
                "tool_ids": [tool["id"]],
            },
        ).json()["skill"]

        unauthorized = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": other_tool["id"],
                "agent_definition_id": agent["id"],
                "input": {"query": "neo"},
            },
        ).json()["call"]
        self.assertEqual(unauthorized["status"], "blocked")

        blocked_by_skill = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": other_tool["id"],
                "agent_definition_id": agent["id"],
                "skill_id": skill["id"],
                "input": {"query": "neo"},
            },
        ).json()["call"]
        self.assertEqual(blocked_by_skill["status"], "blocked")

        self.client.delete(f"/api/tools/definitions/{tool['id']}")
        disabled_tool = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": tool["id"],
                "agent_definition_id": agent["id"],
                "input": {"query": "neo"},
            },
        ).json()["call"]
        self.assertEqual(disabled_tool["status"], "blocked")

        enabled_tool = self._tool(server["id"], name="server_disabled_search")
        agent2 = self._agent(tools=[enabled_tool["id"]])
        self.client.delete(f"/api/tools/servers/{server['id']}")
        disabled_server = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": enabled_tool["id"],
                "agent_definition_id": agent2["id"],
                "input": {"query": "neo"},
            },
        ).json()["call"]
        self.assertEqual(disabled_server["status"], "blocked")

    def test_mcp_discovery_health_uses_argv_shell_false_and_does_not_leak_env_refs(self):
        os.environ["NEO_SECRET_VALUE"] = "super-secret"
        server = self._server(
            command_json=["python", "--version"],
            env_json={"API_KEY": "NEO_SECRET_VALUE"},
            metadata={
                "mock_tools": [
                    {
                        "name": "docs_query",
                        "category": "external_read",
                        "input_schema": {"type": "object"},
                    }
                ]
            },
        )
        completed = subprocess.CompletedProcess(server["command_json"], 0, "super-secret", "")
        with patch("app.services.tools.mcp.subprocess.run", return_value=completed) as run:
            health = self.client.post(f"/api/tools/servers/{server['id']}/health").json()["health"]
        self.assertTrue(health["ok"])
        self.assertNotIn("super-secret", str(health))
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertEqual(run.call_args.args[0], ["python", "--version"])

        discovered = self.client.post(f"/api/tools/servers/{server['id']}/discover")
        self.assertEqual(discovered.status_code, 200, discovered.text)
        self.assertEqual(discovered.json()["definitions"][0]["name"], "docs_query")

    def test_pending_tool_approval_survives_service_restart(self):
        agent = self._agent(tools=["builtin.create_note"])
        pending = self.client.post(
            "/api/tools/calls",
            json={
                "tool_id": "builtin.create_note",
                "agent_definition_id": agent["id"],
                "input": {"title": "Persist me"},
            },
        ).json()["call"]
        fresh_client = TestClient(create_app())
        persisted = fresh_client.get(f"/api/tools/calls/{pending['id']}").json()["call"]
        self.assertEqual(persisted["status"], "pending_approval")
        self.assertEqual(persisted["approval_status"], "pending")


if __name__ == "__main__":
    unittest.main()
