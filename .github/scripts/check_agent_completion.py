"""A file task the model marks as final ends on its checked save.

Writing the file took milliseconds; what made Agent Mode unusable was what came
after it -- a second model turn repeating the code in chat, then a completion
check that sent the run back to test a standalone file. These checks hold the
shortcut to its conditions: every call in the batch ran and was verified, nothing
on the checklist is open, no evidence failed, and the run was not cancelled.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.agent_core import store
from app.services.agent_core.approvals import arguments_hash
from app.services.agent_core.loop import AgentLoop, resume_after_approval
from app.services.agent_core.tool_protocol import ModelTurn
from app.services.agent_core.tools import files
from app.services.agent_core.types import Evidence, StopVerdict, ToolCall
from app.services.disclosure import public


class CompletionChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in [
            ("app.services.agent_core.store._db_path", str(self.root / "agent.db")),
            ("app.services.agent_core.tools.files.repo_root", self.root),
            ("app.services.agent_core.tools.files.is_live", False),
            ("app.services.agent_core.loop.is_live", False),
            ("app.services.agent_core.tools.registry.is_live", False),
            ("app.services.agent_core.loop.launch.sandbox_available", False),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        store.initialize_agent_core_tables()
        self.sid = store.new_id()
        now = store.now_iso()
        store.insert_session({
            "id": self.sid, "objective": "Create hello.txt containing hello",
            "title": "File task", "mode": "auto", "status": "running",
            "created_at": now, "updated_at": now, "repo_id": "temp-workspace",
        })
        store.update_session(self.sid, {"egress_tags": []})
        self.judge = Mock()
        self.judge.adjudicate.return_value = StopVerdict(
            stop_reason="unverified_complete", summary="Stopped without verified completion."
        )
        self.loop = AgentLoop(llm_factory=Mock(), adjudicator=self.judge)
        self.loop._think = Mock()

    def call(self, path="hello.txt", final=True, name="write_file", **kwargs):
        return ToolCall(id=store.new_id(), name=name, arguments={
            "path": path, "content": "hello\n", "complete_task": final, **kwargs,
        })

    def turn(self, *calls):
        return ModelTurn(tool_calls=list(calls), source_label=public("builtin:test"))

    def run_calls(self, *calls):
        self.loop._think.side_effect = [self.turn(*calls), ModelTurn(content="Finished.")]
        return self.loop.run(self.sid)

    def test_final_write_ends_after_one_model_turn(self):
        done = self.run_calls(self.call())
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.stop_reason, "verified_complete")
        self.assertEqual((self.root / "hello.txt").read_text(), "hello\n")
        self.assertEqual(self.loop._think.call_count, 1)
        self.judge.adjudicate.assert_not_called()
        self.assertEqual(store.list_events(self.sid)[-1]["type"], "run.completed")
        self.assertIn("execution was not checked", done.summary)

    def approve_pending(self):
        conn = store._connect()
        try:
            row = conn.execute(
                "SELECT id FROM workspace_agent_approvals WHERE session_id=? AND status='pending'",
                (self.sid,),
            ).fetchone()
            conn.execute(
                "UPDATE workspace_agent_approvals SET status='approved' WHERE id=?", (row["id"],)
            )
            conn.commit()
        finally:
            conn.close()
        return row["id"]

    def test_final_directory_ends_after_one_model_turn(self):
        # Creating a folder asks even in auto mode; the approved call then ends it.
        folder = ToolCall(id=store.new_id(), name="create_directory",
                          arguments={"path": "a", "complete_task": True})
        self.loop._think.side_effect = [self.turn(folder)]
        self.assertEqual(self.loop.run(self.sid).status, "waiting_approval")
        done = resume_after_approval(self.loop, self.sid, self.approve_pending())
        self.assertEqual(done.stop_reason, "verified_complete")
        self.assertTrue((self.root / "a").is_dir())
        self.assertEqual(self.loop._think.call_count, 1)
        self.judge.adjudicate.assert_not_called()

    def test_successful_write_without_signal_continues(self):
        self.run_calls(self.call(final=False))
        self.assertEqual(self.loop._think.call_count, 2)
        self.judge.adjudicate.assert_called_once()

    def test_all_files_in_batch_are_written_before_completion(self):
        done = self.run_calls(self.call("a.txt", False), self.call("b.txt"))
        self.assertEqual(done.stop_reason, "verified_complete")
        self.assertTrue((self.root / "a.txt").exists() and (self.root / "b.txt").exists())
        self.assertEqual(self.loop._think.call_count, 1)

    def test_early_signal_cannot_skip_later_action(self):
        self.run_calls(self.call("a.txt"), self.call("b.txt", False))
        self.assertTrue((self.root / "b.txt").exists())
        self.assertEqual(self.loop._think.call_count, 2)

    def test_failed_save_cannot_finish(self):
        done = self.run_calls(self.call(content=123))
        self.assertNotEqual(done.stop_reason, "verified_complete")
        self.assertFalse((self.root / "hello.txt").exists())
        self.assertEqual(self.loop._think.call_count, 2)

    def test_pending_checklist_blocks_shortcut(self):
        store.update_session(self.sid, {"todo": [
            {"title": "Test requested behavior", "status": "pending"},
        ]})
        self.run_calls(self.call())
        self.assertEqual(self.loop._think.call_count, 2)

    def test_failed_test_evidence_blocks_shortcut(self):
        store.update_session(self.sid, {"evidence": [
            Evidence(kind="tests", tool_call_id="test-failed", passed=False).model_dump(),
        ]})
        self.run_calls(self.call())
        self.assertEqual(self.loop._think.call_count, 2)

    def test_plan_does_not_write_or_claim_success(self):
        store.update_session(self.sid, {"mode": "plan"})
        done = self.run_calls(self.call())
        self.assertFalse((self.root / "hello.txt").exists())
        self.assertNotEqual(done.stop_reason, "verified_complete")

    def test_approval_resume_finishes_without_more_inference(self):
        call = self.call()
        store.update_session(self.sid, {"mode": "normal"})
        self.loop._think.side_effect = [self.turn(call)]
        waiting = self.loop.run(self.sid)
        self.assertEqual(waiting.status, "waiting_approval")
        self.assertFalse((self.root / "hello.txt").exists())
        approval_id = self.approve_pending()
        self.assertEqual(
            arguments_hash(call.arguments), store.get_approval(approval_id)["arguments_hash"]
        )
        done = resume_after_approval(self.loop, self.sid, approval_id)
        self.assertEqual(done.status, "completed")
        self.assertEqual(self.loop._think.call_count, 1)
        self.judge.adjudicate.assert_not_called()

    def test_cancel_during_model_call_cannot_write(self):
        def cancel(_):
            store.cancel_session(self.sid)
            return self.turn(self.call())

        self.loop._think.side_effect = cancel
        self.assertEqual(self.loop.run(self.sid).status, "cancelled")
        self.assertFalse((self.root / "hello.txt").exists())

    def test_readback_detects_mismatched_save(self):
        with patch.object(files.textio, "write_text",
                          side_effect=lambda path, content: path.write_text("wrong")):
            done = self.run_calls(self.call())
        self.assertNotEqual(done.stop_reason, "verified_complete")

    def test_edit_preserves_legacy_encoding_and_crlf(self):
        path = self.root / "legacy.txt"
        path.write_bytes("café\r\nold\r\n".encode("cp1252"))
        call = self.call("legacy.txt", name="edit_file", old_string="old", new_string="new")
        done = self.run_calls(call)
        self.assertEqual(done.stop_reason, "verified_complete")
        self.assertEqual(path.read_bytes(), "café\r\nnew\r\n".encode("cp1252"))

    def test_string_true_is_not_a_completion_signal(self):
        self.run_calls(self.call(final="true"))
        self.assertEqual(self.loop._think.call_count, 2)

    def test_truncated_tool_turn_cannot_finish_task(self):
        turn = self.turn(self.call())
        turn.finish_reason = "length"
        self.loop._think.side_effect = [turn, ModelTurn(content="Finished.")]
        self.loop.run(self.sid)
        self.assertEqual(self.loop._think.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
