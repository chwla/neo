"""Focused smoke checks for folder browsing and Agent Mode tool execution."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.routes import repos
from app.services.agent_core.tool_protocol import request_tool_calls
from app.services.agent_core.tools.base import ToolContext
from app.services.agent_core.tools.registry import ToolRegistry
from app.services.llm import LLMChatResult, LLMMessage
from app.services.repos import safety


class TextModel:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def supports_tools(self):
        return False

    def chat_with_metadata(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        return next(self.responses)


class FolderAgentChecks(unittest.TestCase):
    def test_host_picker_uses_home_but_keeps_it_browse_only(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as scratch:
            home = Path(scratch)
            project = home / "project"
            project.mkdir()
            with patch("app.api.routes.repos.Path.home", return_value=home), patch(
                "app.api.routes.repos._configured_roots", return_value=[]
            ), patch("app.api.routes.repos.in_container", return_value=False), patch(
                "app.api.routes.repos.store.list_repos", return_value=([], 0)
            ), patch("app.services.repos.safety.Path.home", return_value=home), patch(
                "app.services.repos.safety.in_container", return_value=False
            ), patch("app.services.repos.safety._live_roots", return_value=[]):
                view = repos.browse_folders()
                self.assertEqual(view["path"], str(home))
                self.assertEqual([item["name"] for item in view["entries"]], ["project"])
                self.assertFalse(view["can_attach"])
                self.assertEqual(safety.validate_browse_path(str(project)), project)
                self.assertEqual(repos.list_roots()["roots"], [str(home)])

    def test_unconfigured_container_stays_closed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as scratch, patch(
            "app.services.repos.safety._live_roots", return_value=[]
        ), patch("app.services.repos.safety.in_container", return_value=True):
            with self.assertRaisesRegex(ValueError, "No workspace root is configured"):
                safety.validate_browse_path(scratch)

    def test_agent_takes_bare_text_call_then_creates_directory(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as scratch:
            root = Path(scratch)
            model = TextModel(
                LLMChatResult(content='{"name":"create_directory","parameters":{"path":"A"}}'),
            )
            registry = ToolRegistry()
            schemas = registry.schemas(mode="normal", has_repo=True, live=True)
            turn = request_tool_calls(
                model, [LLMMessage(role="user", content="create A")],
                schemas,
            )
            self.assertEqual([call.name for call in turn.tool_calls], ["create_directory"])
            self.assertEqual(len(model.requests), 1)

            context = ToolContext(session_id="smoke", repo_id="repo", extras={})
            with patch("app.services.agent_core.tools.files.repo_root", return_value=root), patch(
                "app.services.agent_core.tools.files.is_live", return_value=True
            ):
                result = registry.execute(turn.tool_calls[0], context)
            self.assertEqual(result.status, "ok")
            self.assertTrue(result.evidence.passed)
            self.assertTrue((root / "A").is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
