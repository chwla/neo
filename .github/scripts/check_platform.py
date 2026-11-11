"""Behaviour that differs by platform unless held to the same outcome everywhere.

* the agent loop runs every tool call of a batch, even after one paused for approval,
  and a session grant keeps applying after the pause;
* a ``path_prefix`` grant covers a folder, not every name that starts with its letters;
* the date-and-time answer formats on Windows' C runtime as well as glibc's;
* usernames refuse control and invisible characters;
* bundle redaction catches token-shaped values inside free text;
* the command policy refuses what ``cmd.exe`` would reinterpret, and no batch shim runs.
"""

import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.core.child_env import ProgramRefused, child_environment, resolve_program
from app.services.agent_core import permissions
from app.services.agent_core.tools.base import AgentTool
from app.services.agent_core.types import Grant
from app.services.bundles.redaction import redact
from app.services.command_sandbox import policy
from app.services.profile_accounts import _normalize_username
from app.services.search.live_data import local_datetime_answer

WINDOWS = os.name == "nt"


class AgentLoopChecks(unittest.TestCase):
    """The loop against an in-memory store and a scripted model."""

    def setUp(self):
        self._scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self._scratch.cleanup)
        root = Path(self._scratch.name).resolve()
        # Every store reads the database through the profile ContextVar; pointing
        # it at a scratch file keeps this check off the real data.
        from app.core.config import active_profile_database_url, active_profile_storage_dir

        token = active_profile_database_url.set(f"sqlite:///{root / 'neo.db'}")
        self.addCleanup(active_profile_database_url.reset, token)
        (root / "profile").mkdir()
        (root / "elsewhere").mkdir()  # the original folder of the managed copy
        storage = active_profile_storage_dir.set(str(root / "profile"))
        self.addCleanup(active_profile_storage_dir.reset, storage)
        from app.services.agent_core import store
        from app.services.files.store import initialize_workspace_file_tables
        from app.services.projects.store import initialize_project_tables
        from app.services.repos import store as repos_store

        store.initialize_agent_core_tables()
        initialize_project_tables()
        initialize_workspace_file_tables()
        self.store = store
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "a.txt").write_text("alpha", encoding="utf-8")
        # A registered repository, so the run's starting context is classified
        # (public) rather than left unknown, which would gate every write.
        repos_store.insert_repo({
            "id": "repo-check", "name": "check", "original_path": str(root / "elsewhere"),
            "workspace_path": str(self.workspace), "access": "managed", "status": "ready",
            "created_at": store.now_iso(), "updated_at": store.now_iso(),
        })
        from app.services.disclosure import public
        from app.services.disclosure.artifacts import ArtifactLabels

        ArtifactLabels(root / "profile" / "artifact-labels.json").raise_label(
            "repo:repo-check", public("workspace:repo-check")
        )
        root_patch = patch(
            "app.services.agent_core.tools.files.repo_root", return_value=self.workspace
        )
        root_patch.start()
        self.addCleanup(root_patch.stop)
        for target in ("app.services.agent_core.tools.files.is_live",
                       "app.services.agent_core.loop.is_live"):
            live = patch(target, return_value=False)
            live.start()
            self.addCleanup(live.stop)
        has = patch("app.services.agent_core.loop.has_workspace", return_value=True)
        has.start()
        self.addCleanup(has.stop)

    def _loop(self, *turns):
        from app.services.agent_core.loop import AgentLoop
        from app.services.agent_core.tool_protocol import ModelTurn
        from app.services.agent_core.types import ToolCall
        from app.services.disclosure import public

        scripted = []
        for index, calls in enumerate(turns):
            scripted.append(ModelTurn(
                content="" if calls else "Done.",
                tool_calls=[ToolCall(id=f"call-{index}-{i}-{c['name']}", name=c["name"],
                                     arguments=c["arguments"]) for i, c in enumerate(calls)],
                source_label=public("user_input:check-platform"),
            ))
        requests = []

        def fake_request(client, messages, schemas):
            requests.append(messages)
            return scripted.pop(0)

        class Model:
            def supports_tools(self):
                return True

        class Adjudicator:
            def adjudicate(self, session, transcript):
                from app.services.agent_core.completion import StopVerdict

                return StopVerdict(stop_reason="unverified_complete", summary="done")

        loop = AgentLoop(llm_factory=Model, adjudicator=Adjudicator())
        patcher = patch("app.services.agent_core.loop.request_tool_calls", side_effect=fake_request)
        patcher.start()
        self.addCleanup(patcher.stop)
        return loop, requests

    def _session(self, mode="normal"):
        session = self.store.insert_session({
            "id": self.store.new_id(), "objective": "edit files", "title": "check",
            "mode": mode, "repo_id": "repo-check", "status": "queued",
            "created_at": self.store.now_iso(), "updated_at": self.store.now_iso(),
        })
        return session["id"]

    def test_batch_after_an_approval_is_fully_executed(self):
        from app.services.agent_core.loop import resume_after_approval

        loop, requests = self._loop(
            [
                {"name": "write_file", "arguments": {"path": "src/one.txt", "content": "1"}},
                {"name": "read_file", "arguments": {"path": "src/a.txt"}},
                {"name": "list_dir", "arguments": {"path": "src"}},
            ],
            [{"name": "write_file", "arguments": {"path": "src/two.txt", "content": "2"}}],
            [],
        )
        session_id = self._session()
        loop.run(session_id)
        pending = self.store.pending_approval(session_id)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["tool_name"], "write_file")
        # Nothing but the approval request has run yet.
        self.assertEqual(self.store.list_tool_calls(session_id), [])

        # ``AgentCoreService.decide`` would hand the resume to a worker thread; the
        # same grant-then-resume is driven inline here so the outcome can be read.
        self.store.insert_grant({
            "id": self.store.new_id(), "session_id": session_id, "tool_name": "write_file",
            "predicate": {"kind": "path_prefix", "argument": "path", "value": "src"},
            "created_at": self.store.now_iso(),
        })
        self.store.decide_approval(pending["id"], True)
        resume_after_approval(loop, session_id, pending["id"])

        recorded = [(c["tool_name"], c["status"]) for c in self.store.list_tool_calls(session_id)]
        self.assertEqual(
            recorded,
            [("write_file", "ok"), ("read_file", "ok"), ("list_dir", "ok"), ("write_file", "ok")],
        )
        # The second write was covered by the grant: no second approval, and the
        # run kept its public starting context rather than becoming restricted.
        self.assertIsNone(self.store.pending_approval(session_id))
        session = self.store.get_session(session_id)
        self.assertEqual(session["status"], "completed", session)
        self.assertEqual(session["egress_tags"], [])
        self.assertTrue((self.workspace / "src" / "two.txt").exists())
        # Every tool call id in the transcript has exactly one result before the
        # next model request, which is the invariant strict providers enforce.
        tool_ids = [m["tool_call_id"] for m in self.store.list_messages(session_id)
                    if m["role"] == "tool"]
        self.assertEqual(sorted(tool_ids), sorted(set(tool_ids)))
        asked = [m for m in self.store.list_messages(session_id) if m["role"] == "assistant"]
        for message in asked:
            for call in message.get("tool_calls") or []:
                self.assertIn(call["id"], tool_ids)


class RecoveryChecks(AgentLoopChecks):
    def test_abandoned_runs_are_interrupted_and_live_ones_kept(self):
        from app.services.agent_core import worker

        dead = self._session()
        alive = self._session()
        stale = "2000-01-01T00:00:00"
        self.store.update_session(dead, {"status": "running", "heartbeat_at": stale})
        fresh = self.store.now_iso()
        self.store.update_session(alive, {"status": "running", "heartbeat_at": fresh})
        # The sweep leaves a run that is still heartbeating alone.
        self.assertEqual(self.store.interrupt_abandoned_sessions(only_stale=True), [dead])
        self.assertEqual(self.store.get_session(dead)["status"], "interrupted")
        self.assertEqual(self.store.get_session(alive)["status"], "running")
        # At startup every running row is a previous process's, unless this
        # process is running it right now.
        with patch.object(worker, "active_ids", return_value={alive}):
            self.assertEqual(
                self.store.interrupt_abandoned_sessions(only_stale=False, keep=worker.active_ids()),
                [],
            )
        self.assertEqual(self.store.interrupt_abandoned_sessions(only_stale=False), [alive])
        from app.services.agent_core.service import (
            AgentCoreConflict,
            AgentCoreService,
            ApprovalDecision,
        )

        with self.assertRaises(AgentCoreConflict):
            AgentCoreService().decide(dead, "nope", ApprovalDecision(decision="allow_once"))


class SlotChecks(unittest.TestCase):
    def test_turn_slots_are_counted_per_profile(self):
        from app.core.config import active_profile_storage_dir
        from app.services.agent_core import worker

        with patch.dict(worker._ACTIVE, {"run-a": "profile-a", "run-b": "profile-b"}, clear=True):
            token = active_profile_storage_dir.set("profile-a")
            try:
                self.assertEqual(worker.slot_holders(), {"run-a"})
                self.assertEqual(worker.active_ids(), {"run-a", "run-b"})
                worker.release_slot("run-a")
                self.assertEqual(worker.slot_holders(), set())
            finally:
                active_profile_storage_dir.reset(token)
                worker._RELEASED.clear()


class GrantChecks(unittest.TestCase):
    def test_path_prefix_grant_covers_the_folder_only(self):
        tool = AgentTool(name="write_file", description="", parameters={}, risk="workspace_write",
                         requires_repo=True, handler=lambda a, c: "", path_arguments=("path",))
        grant = Grant(id="g", session_id="s", tool_name="write_file",
                      predicate={"kind": "path_prefix", "argument": "path", "value": "src"},
                      created_at="now")
        self.assertTrue(permissions.grant_matches(grant, tool, {"path": "src/d.txt"}))
        self.assertTrue(permissions.grant_matches(grant, tool, {"path": "src/deep/d.txt"}))
        self.assertFalse(permissions.grant_matches(grant, tool, {"path": "src_private/d.txt"}))
        self.assertFalse(permissions.grant_matches(grant, tool, {"path": "src/../secrets"}))
        self.assertFalse(permissions.grant_matches(grant, tool, {"path": "SRC2/d.txt"}))

    def test_legacy_only_restriction_does_not_gate_workspace_writes(self):
        from app.services.disclosure import Label

        tool = AgentTool(name="write_file", description="", parameters={}, risk="workspace_write",
                         requires_repo=True, handler=lambda a, c: "", path_arguments=("path",))
        egress = permissions.EgressContext(label=Label.private(permissions.LEGACY_CONTEXT_TAG))
        decision = permissions.PermissionResolver().resolve(
            tool=tool, arguments={"path": "x.txt"}, mode="auto", egress=egress)
        self.assertEqual(decision.outcome, "allow")
        memory = permissions.EgressContext(label=Label.private("memory"))
        asked = permissions.PermissionResolver().resolve(
            tool=tool, arguments={"path": "x.txt"}, mode="auto", egress=memory)
        self.assertEqual(asked.outcome, "ask")
        self.assertIn("your memories", asked.reason)
        self.assertNotIn("legacy_context", asked.reason)


class DateTimeChecks(unittest.TestCase):
    def test_answers_format_on_every_c_runtime(self):
        now = datetime(2026, 11, 3, 7, 5, tzinfo=UTC)
        for query in ("what time is it", "what is the date today", "what is the date and time"):
            result = local_datetime_answer(query, browser_timezone="Asia/Kolkata", now=now)
            self.assertIn("Asia/Kolkata", result.answer)
        at = local_datetime_answer("what is the date and time", browser_timezone="Asia/Kolkata",
                                   now=now)
        self.assertIn("November 3, 2026 at 12:35 PM", at.answer)
        self.assertEqual(at.instant.tzinfo, ZoneInfo("Asia/Kolkata"))


class UsernameChecks(unittest.TestCase):
    def test_control_and_invisible_characters_are_refused(self):
        for bad in ("x\x00y", "x\x1by", "a\u202eb", "zero\u200bwidth", "soft\xadhyphen"):
            with self.subTest(name=repr(bad)):
                with self.assertRaises(HTTPException) as caught:
                    _normalize_username(bad)
                self.assertEqual(caught.exception.status_code, 422)
        # A line separator is whitespace to ``str.split`` and collapses to a space.
        self.assertEqual(_normalize_username("line\u2028break"), "line break")
        self.assertEqual(_normalize_username("Zo\xeb \U0001f600"), "Zo\xeb \U0001f600")
        self.assertEqual(_normalize_username("e\u0301"), "\xe9")  # NFC


class RedactionChecks(unittest.TestCase):
    def test_token_shaped_values_in_free_text_are_redacted(self):
        text = (
            "Use sk-or-v1-0123456789abcdef0123456789abcdef0123456789abcdef to call OpenRouter, "
            "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 for GitHub, and password=hunter22x."
        )
        out = redact({"description": text, "notes": ["Bearer abcdefghijklmnopqrstuvwxyz0123"]})
        self.assertNotIn("sk-or-v1-", out["description"])
        self.assertNotIn("ghp_", out["description"])
        self.assertNotIn("hunter22x", out["description"])
        self.assertIn("password=[REDACTED]", out["description"])
        self.assertIn("OpenRouter", out["description"])
        self.assertEqual(out["notes"], ["[REDACTED]"])
        self.assertEqual(redact({"plain": "nothing secret here"})["plain"], "nothing secret here")


class CommandPolicyChecks(unittest.TestCase):
    def test_cmd_metacharacters_are_refused(self):
        for argument in ("x&calc", "a^b", "%PATH%", "!x!", 'say "hi"', "a\nb"):
            with self.subTest(argument=argument):
                decision = policy.validate(["npm", "test", "--", argument], "test", ".")
                self.assertFalse(decision["allowed"], decision)
        self.assertTrue(policy.validate(["npm", "test"], "test", ".")["allowed"])

    def test_child_environment_has_what_windows_programs_need(self):
        env = child_environment(Path.cwd())
        self.assertIn("PATH", env)
        if WINDOWS:
            for name in ("SystemRoot", "TEMP", "PATHEXT", "USERPROFILE"):
                self.assertIn(name, env)
            self.assertEqual(env["USERPROFILE"], str(Path.cwd()))
        else:
            self.assertEqual(env["HOME"], str(Path.cwd()))

    @unittest.skipUnless(WINDOWS, "batch shims are a Windows matter")
    def test_batch_shims_never_run(self):
        with tempfile.TemporaryDirectory() as scratch:
            bin_dir = Path(scratch) / "bin"
            bin_dir.mkdir()
            marker = Path(scratch) / "marker.txt"
            (bin_dir / "ruff.cmd").write_text(f'@echo pwned> "{marker}"\r\n', encoding="utf-8")
            with self.assertRaises(ProgramRefused):
                resolve_program("ruff", Path(scratch) / "ws", str(bin_dir))
            self.assertFalse(marker.exists())
            # npm's own shim is replaced by node plus its script when node is there.
            import shutil

            if shutil.which("npm.cmd") and shutil.which("node"):
                launch = resolve_program("npm", Path(scratch) / "ws", os.environ.get("PATH", ""))
                self.assertTrue(launch[0].lower().endswith("node.exe"), launch)
                self.assertTrue(launch[1].lower().endswith("npm-cli.js"), launch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
