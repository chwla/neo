"""Focused checks for the Tools menu and tool protocol on each supported OS."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.agent_core.permissions import PermissionResolver
from app.services.agent_core.tool_protocol import ToolProtocolError, request_tool_calls
from app.services.agent_core.tools.base import ToolContext
from app.services.agent_core.tools.connectors import chat_tools_catalog
from app.services.agent_core.tools.registry import ToolRegistry
from app.services.agent_core.types import ToolCall
from app.services.llm import LLMChatResult, normalize_tool_calls


class Model:
    def __init__(self, *responses, native=False):
        self.responses = iter(responses)
        self.native = native
        self.requests = []

    def supports_tools(self):
        return self.native

    def chat_with_metadata(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        return next(self.responses)


class AgentToolsChecks(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

    def test_menu_matches_exposed_tools_with_and_without_folder(self):
        legacy_disabled = {"read_file", "list_dir", "write_file", "edit_file", "delete_file"}
        for has_repo, disabled in [(False, set()), (True, set()), (True, legacy_disabled)]:
            with self.subTest(has_repo=has_repo, disabled=disabled):
                scope = {"mode": "normal", "has_repo": has_repo, "live": has_repo}
                catalog = chat_tools_catalog(self.registry, disabled, **scope)
                schema_names = {
                    schema["function"]["name"] for schema in
                    self.registry.schemas(disabled=frozenset(disabled), **scope)
                }
                self.assertEqual(
                    {item["name"] for item in catalog if item["available"]}, schema_names
                )
                mkdir = next(item for item in catalog if item["name"] == "create_directory")
                self.assertEqual(mkdir["enabled"], not disabled)
                reason = None if has_repo else "folder_required"
                self.assertEqual(mkdir["unavailable_reason"], reason)
                self.assertIn("web_search", schema_names)

    def test_directory_creation_and_containment(self):
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch).resolve()
            root = base / "Desktop"
            root.mkdir()
            (root / "file.txt").write_text("keep", encoding="utf-8")
            with patch("app.services.agent_core.tools.files.repo_root", return_value=root), patch(
                "app.services.agent_core.tools.files.is_live", return_value=True
            ):
                context = ToolContext(session_id="smoke", repo_id="folder")

                def mkdir(path):
                    return self.registry.execute(
                        ToolCall(id="test-call", name="create_directory", arguments={"path": path}),
                        context,
                    )

                self.assertTrue(mkdir("A").evidence.passed)
                self.assertEqual(mkdir("A").status, "ok")
                self.assertEqual(list((root / "A").iterdir()), [])
                for invalid in ("../escape", str(base / "escape"), "missing/child", "file.txt"):
                    with self.subTest(invalid=invalid):
                        self.assertEqual(mkdir(invalid).status, "error")
                self.assertFalse((base / "escape").exists())
                self.assertFalse((root / "missing").exists())
                self.assertEqual((root / "file.txt").read_text(encoding="utf-8"), "keep")
                if os.name != "nt":
                    (root / "link").symlink_to(base, target_is_directory=True)
                    self.assertEqual(mkdir("link/escape").status, "error")
                    self.assertFalse((base / "escape").exists())

    def test_write_modes_and_approval(self):
        tool = self.registry.get("create_directory")
        resolver = PermissionResolver()
        for mode, expected in [("plan", "propose"), ("normal", "ask"), ("auto", "ask")]:
            decision = resolver.resolve(tool=tool, arguments={"path": "A"}, mode=mode)
            self.assertEqual(decision.outcome, expected)

    def test_text_tool_recovery(self):
        schemas = self.registry.schemas(mode="normal", has_repo=True)
        good = LLMChatResult(content='{"tool_calls":[{"name":"create_directory",'
                                    '"arguments":{"path":"A"}}]}')
        for bad in (
            '{"name":"mkdir","parameters":{"path":"A"}}',
            '{"tool_calls":[{"name":"create_directory","arguments":{"path":"A"}',
            '{"tool_calls":[{"name":"create_calendar_event",'
            '"arguments":{"title":"Create folder"}}]}',
        ):
            with self.subTest(bad=bad):
                client = Model(LLMChatResult(content=bad), good)
                result = request_tool_calls(client, [], schemas)
                self.assertEqual([call.name for call in result.tool_calls], ["create_directory"])
                self.assertEqual(result.content, "")
                self.assertEqual(len(client.requests), 2)

    def test_native_invalid_arguments_are_never_dispatched(self):
        schemas = self.registry.schemas(mode="normal", has_repo=True)
        raw = [{"id": "bad", "function": {"name": "create_directory", "arguments": '{"path":'}}]
        bad = LLMChatResult(content="", tool_calls=normalize_tool_calls(raw))
        client = Model(bad, bad, native=True)
        with self.assertRaises(ToolProtocolError):
            request_tool_calls(client, [], schemas)
        self.assertEqual(len(client.requests), 2)
        self.assertIn("tools", client.requests[0][1])

    def test_prose_with_json_example_is_left_intact(self):
        prose = 'Example settings: ```json\n{"workspace": "Desktop"}\n```'
        client = Model(LLMChatResult(content=prose))
        schemas = self.registry.schemas(mode="normal", has_repo=False)
        result = request_tool_calls(client, [], schemas)
        self.assertEqual(result.content, prose)
        self.assertEqual(len(client.requests), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
