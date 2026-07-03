import os
import tempfile
import unittest
import uuid
from types import SimpleNamespace

from app.core.config import get_settings
from app.services.agents import AgentRunCreate, AgentsService, SaveRunToNoteRequest
from app.services.agents.guidance import agent_run_guidance
from app.services.agents.runner import AgentRunner
from app.services.agents.store import initialize_agent_tables, insert_step, list_steps, now_iso, recover_interrupted_runs
from app.services.notes import NoteCreate, NotesService
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.search.types import WebContext
from app.services.tasks import TaskCreate, TasksService
from app.services.tasks.store import initialize_task_tables


class FakeLLM:
    def __init__(self, fail=False):
        self.fail = fail

    def chat_with_metadata(self, messages, temperature=0.2, num_predict=None):
        if self.fail:
            raise RuntimeError("model failure sentinel")
        prompt = messages[-1].content
        if "Create the task deliverable" in prompt:
            content = "Summary\nA safe task output.\n\nNext steps\nReview the draft."
        else:
            content = "The task needs a scoped deliverable and has no destructive actions."
        return SimpleNamespace(content=content)


class UnavailableWeb:
    def build_context_forced(self, query):
        return WebContext(query=query, needed=True, warning="SearXNG unavailable sentinel")


class ImmediateRunner:
    def __init__(self, *, fail=False):
        self.runner = AgentRunner(
            llm_factory=lambda: FakeLLM(fail=fail),
            web_factory=UnavailableWeb,
        )

    def start(self, run_id):
        self.runner.run_sync(run_id)


class PassiveRunner:
    def __init__(self):
        self.started = []

    def start(self, run_id):
        self.started.append(run_id)


class AgentGuidanceTest(unittest.TestCase):
    def test_explicit_agent_request_returns_manual_navigation_only(self):
        reply = agent_run_guidance("Run agent on this task")
        self.assertIn("Open Tasks", reply)
        self.assertIn("does not start agent runs automatically", reply)
        self.assertIn("Open Tasks", agent_run_guidance("Start working on task Neo API"))

    def test_unrelated_chat_does_not_trigger_guidance(self):
        self.assertIsNone(agent_run_guidance("Summarize this task"))


class AgentsServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmpdir.name}/agents.db"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        initialize_agent_tables()
        self.notes = NotesService()
        self.projects = ProjectsService()
        self.tasks = TasksService()
        self.project = self.projects.create_project(ProjectCreate(title="Neo Agent Test"))
        self.note = self.notes.create_note(NoteCreate(title="Agent context", body="Important linked context."))
        self.task = self.tasks.create_task(TaskCreate(
            title="Draft implementation plan",
            description="Prepare a safe coding plan.",
            project_id=self.project.id,
        ))
        self.tasks.attach_note(self.task.id, self.note.id)

    def tearDown(self):
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        self.tmpdir.cleanup()

    def test_completed_run_logs_context_plan_steps_output_and_persists(self):
        service = AgentsService(runner=ImmediateRunner())
        created = service.create_run(AgentRunCreate(task_id=self.task.id))
        result = service.read_run(created.id)
        self.assertIsNotNone(result)
        run, steps, artifacts = result
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.task_id, self.task.id)
        self.assertEqual(run.project_id, self.project.id)
        self.assertIn("Summary", run.final_output)
        self.assertGreaterEqual(len(run.plan), 3)
        self.assertEqual([step.step_index for step in steps], list(range(len(steps))))
        self.assertTrue(all(step.status == "completed" for step in steps))
        context = next(step.output_text for step in steps if step.step_type == "read_context")
        self.assertIn(self.task.title, context)
        self.assertIn(self.project.title, context)
        self.assertIn(self.note.title, context)
        self.assertTrue(any(artifact.artifact_type == "final_output" for artifact in artifacts))

        persisted, total = AgentsService(runner=PassiveRunner()).list_runs(task_id=self.task.id)
        self.assertEqual(total, 1)
        self.assertEqual(persisted[0].id, run.id)

    def test_web_unavailable_is_logged_and_run_still_completes(self):
        service = AgentsService(runner=ImmediateRunner())
        run = service.create_run(AgentRunCreate(
            task_id=self.task.id,
            objective="Research the latest implementation options",
        ))
        completed, steps, _ = service.read_run(run.id)
        self.assertEqual(completed.status, "completed")
        web_step = next(step for step in steps if step.step_type == "web_search")
        self.assertEqual(web_step.status, "completed")
        self.assertIn("SearXNG unavailable sentinel", web_step.output_text)

    def test_failed_step_preserves_logs_and_error(self):
        service = AgentsService(runner=ImmediateRunner(fail=True))
        run = service.create_run(AgentRunCreate(task_id=self.task.id))
        failed, steps, _ = service.read_run(run.id)
        self.assertEqual(failed.status, "failed")
        self.assertIn("model failure sentinel", failed.error)
        failed_steps = [step for step in steps if step.status == "failed"]
        self.assertEqual(len(failed_steps), 1)
        self.assertIn("model failure sentinel", failed_steps[0].error)

    def test_cancel_active_run_preserves_steps(self):
        service = AgentsService(runner=PassiveRunner())
        run = service.create_run(AgentRunCreate(task_id=self.task.id))
        cancelled = service.cancel_run(run.id)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(all(step["status"] == "cancelled" for step in list_steps(run.id)))

    def test_approval_is_explicit_and_does_not_claim_external_execution(self):
        runner = PassiveRunner()
        service = AgentsService(runner=runner)
        run = service.create_run(AgentRunCreate(task_id=self.task.id))
        now = now_iso()
        step = insert_step({
            "id": str(uuid.uuid4()), "run_id": run.id, "step_index": 99,
            "step_type": "save_note", "title": "Request note save",
            "status": "waiting_approval", "input": {}, "output_text": None,
            "error": None, "requires_approval": True, "approval_status": "pending",
            "created_at": now, "updated_at": now, "started_at": now,
            "completed_at": None,
        })
        approved = service.approve_step(run.id, step["id"], True)
        self.assertEqual(approved.approval_status, "approved")
        self.assertIn("no external action was executed", approved.output_text)
        self.assertEqual(runner.started, [run.id, run.id])

    def test_save_output_to_note_is_idempotent_and_links_both_directions(self):
        service = AgentsService(runner=ImmediateRunner())
        run = service.create_run(AgentRunCreate(task_id=self.task.id))
        first, first_existing = service.save_output_to_note(run.id, SaveRunToNoteRequest())
        second, second_existing = service.save_output_to_note(run.id, SaveRunToNoteRequest())
        self.assertFalse(first_existing)
        self.assertTrue(second_existing)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.source_type, "agent_run")
        self.assertEqual(first.source_id, run.id)
        self.assertIn(first.id, [note.id for note in self.tasks.list_task_notes(self.task.id)])
        self.assertIn(first.id, [note.id for note in self.projects.list_project_notes(self.project.id)])
        _, _, artifacts = service.read_run(run.id)
        self.assertEqual(len([item for item in artifacts if item.note_id == first.id]), 1)

    def test_restart_recovery_marks_active_runs_failed_and_preserves_completed(self):
        passive = AgentsService(runner=PassiveRunner())
        active = passive.create_run(AgentRunCreate(task_id=self.task.id))
        completed = AgentsService(runner=ImmediateRunner()).create_run(AgentRunCreate(task_id=self.task.id))
        self.assertGreaterEqual(recover_interrupted_runs(), 1)
        recovered, _, _ = passive.read_run(active.id)
        still_completed, _, _ = passive.read_run(completed.id)
        self.assertEqual(recovered.status, "failed")
        self.assertIn("backend restart", recovered.error)
        self.assertEqual(still_completed.status, "completed")


if __name__ == "__main__":
    unittest.main()
