import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.services.chat import NeoChatService
from app.services.context import ContextPackage
from app.services.notes import NoteCreate, NotesService
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.tasks import TaskContextService, TaskCreate, TasksService, TaskUpdate
from app.services.tasks.service import TasksValidationError
from app.services.tasks.store import initialize_task_tables


class TasksServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmpdir.name}/tasks.db"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        initialize_task_tables()
        self.notes = NotesService()
        self.projects = ProjectsService()
        self.tasks = TasksService()

    def tearDown(self) -> None:
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        self.tmpdir.cleanup()

    def test_crud_search_filters_status_pin_archive_delete_and_persistence(self):
        project = self.projects.create_project(ProjectCreate(title="Neo", priority="high"))
        task = self.tasks.create_task(TaskCreate(
            title="Implement notes search",
            description="Add title body and tag search",
            priority="high",
            due_at="2026-07-10T18:00:00",
            project_id=project.id,
            tags=["Neo", "backend", "neo"],
        ))
        self.assertEqual(task.status, "todo")
        self.assertEqual(task.priority, "high")
        self.assertEqual(task.tags, ["backend", "neo"])

        for filters in ({"q": "notes search"}, {"priority": "high"},
                        {"project_id": project.id}, {"tag": "NEO"},
                        {"due_before": "2026-07-11"}, {"status": "todo"}):
            tasks, total = self.tasks.list_tasks(**filters)
            self.assertEqual(total, 1)
            self.assertEqual(tasks[0].id, task.id)
            self.assertEqual(tasks[0].project_title, "Neo")

        doing = self.tasks.set_status(task.id, "doing")
        self.assertEqual(doing.status, "doing")
        self.assertIsNone(doing.completed_at)
        done = self.tasks.set_status(task.id, "done")
        self.assertIsNotNone(done.completed_at)
        reopened = self.tasks.set_status(task.id, "todo")
        self.assertIsNone(reopened.completed_at)

        updated = self.tasks.update_task(task.id, TaskUpdate(
            title="Implement task search", priority="critical", due_at=None,
            tags=["workspace"],
        ))
        self.assertEqual(updated.priority, "critical")
        self.assertIsNone(updated.due_at)
        self.assertEqual(updated.tags, ["workspace"])

        self.assertTrue(self.tasks.set_pinned(task.id, True).pinned)
        self.assertTrue(self.tasks.set_archived(task.id, True).archived)
        self.assertEqual(self.tasks.list_tasks()[1], 0)
        archived, total = self.tasks.list_tasks(include_archived=True)
        self.assertEqual(total, 1)
        self.assertEqual(archived[0].id, task.id)
        self.assertIsNotNone(TasksService().get_task(task.id))
        self.assertTrue(self.tasks.soft_delete(task.id))
        self.assertEqual(self.tasks.list_tasks(include_archived=True)[1], 0)

    def test_note_linking_is_idempotent_and_project_tasks_are_listable(self):
        project = self.projects.create_project(ProjectCreate(title="Neo"))
        task = self.tasks.create_task(TaskCreate(title="Link task context", project_id=project.id))
        note = self.notes.create_note(NoteCreate(title="Task research", body="Useful context"))

        self.assertTrue(self.tasks.attach_note(task.id, note.id))
        self.assertTrue(self.tasks.attach_note(task.id, note.id))
        linked_notes = self.tasks.list_task_notes(task.id)
        self.assertEqual(len(linked_notes), 1)
        self.assertEqual(linked_notes[0].id, note.id)
        linked_tasks = self.tasks.list_note_tasks(note.id)
        self.assertEqual(len(linked_tasks), 1)
        self.assertEqual(linked_tasks[0].id, task.id)
        project_tasks, total = self.tasks.list_tasks(project_id=project.id)
        self.assertEqual(total, 1)
        self.assertEqual(project_tasks[0].id, task.id)
        self.assertTrue(self.tasks.detach_note(task.id, note.id))
        self.assertEqual(self.tasks.list_task_notes(task.id), [])

    def test_validation_and_task_context_are_scoped(self):
        project = self.projects.create_project(ProjectCreate(title="Neo"))
        blocked = self.tasks.create_task(TaskCreate(
            title="Fix runtime ports", status="blocked", priority="critical", project_id=project.id
        ))
        self.assertEqual(TaskContextService().context_for_prompt("What is the weather?"), "No task context loaded.")
        context = TaskContextService().context_for_prompt("What is blocked for Neo?")
        self.assertIn(blocked.title, context)
        self.assertIn("critical", context)
        direct = TaskContextService().answer_for_prompt("What is blocked for Neo?")
        self.assertIn("Blocked tasks:", direct)
        self.assertIn(blocked.title, direct)
        self.assertIsNone(TaskContextService().answer_for_prompt("Create a task to ship Neo"))
        with self.assertRaises(TasksValidationError):
            self.tasks.create_task(TaskCreate(title="Bad date", due_at="tomorrow"))
        with self.assertRaises(TasksValidationError):
            self.tasks.create_task(TaskCreate(title="x" * 201))

    def test_task_answers_cover_paraphrases_and_do_not_hijack_unrelated_chat(self):
        project = self.projects.create_project(ProjectCreate(title="Neo"))
        doing = self.tasks.create_task(TaskCreate(
            title="Implement Tasks v1", status="doing", priority="high", project_id=project.id
        ))
        done = self.tasks.create_task(TaskCreate(
            title="Fix stale backend port", status="done", priority="high", project_id=project.id
        ))
        blocked = self.tasks.create_task(TaskCreate(
            title="Improve research comparison tests", description="Waiting for fixtures",
            status="blocked", priority="medium", project_id=project.id,
        ))
        critical = self.tasks.create_task(TaskCreate(
            title="Add task filters", status="todo", priority="critical", project_id=project.id,
            due_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        ))
        older = self.tasks.create_task(TaskCreate(title="Older completed task", status="done"))
        self.tasks.update_task(older.id, TaskUpdate(completed_at="2026-01-01T00:00:00+00:00"))
        self.tasks.update_task(done.id, TaskUpdate(completed_at="2026-07-01T00:00:00+00:00"))

        responder = TaskContextService()
        next_answer = responder.answer_for_prompt("What should I focus on next?")
        self.assertIn(doing.title, next_answer)
        self.assertIn(critical.title, next_answer)
        open_answer = responder.answer_for_prompt("Which tasks are open for Neo?")
        self.assertIn(doing.title, open_answer)
        self.assertIn(blocked.title, open_answer)
        self.assertIn(critical.title, open_answer)
        self.assertNotIn(done.title, open_answer)
        self.assertIn(blocked.title, responder.answer_for_prompt("What all is blocked right now?"))
        completed_answer = responder.answer_for_prompt("Which jobs did I finish recently?")
        self.assertLess(completed_answer.index(done.title), completed_answer.index(older.title))
        self.assertIn(critical.title, responder.answer_for_prompt("What is due soon?"))
        self.assertIn(critical.title, responder.answer_for_prompt("Show my critical tasks."))
        self.assertIn(doing.title, responder.answer_for_prompt("Show tasks for Neo."))
        self.assertIsNone(responder.answer_for_prompt("Explain binary search."))
        self.assertIsNone(responder.answer_for_prompt("List tasks in Python asyncio."))

    def test_archived_status_round_trip_has_clean_semantics(self):
        task = self.tasks.create_task(TaskCreate(title="Archive semantics"))
        archived = self.tasks.set_status(task.id, "archived")
        self.assertEqual(archived.status, "archived")
        self.assertTrue(archived.archived)
        restored = self.tasks.set_archived(task.id, False)
        self.assertEqual(restored.status, "todo")
        self.assertFalse(restored.archived)

    def test_chat_prompt_includes_scoped_read_only_task_context(self):
        project = self.projects.create_project(ProjectCreate(title="Neo"))
        self.tasks.create_task(TaskCreate(
            title="Ship Tasks v1", status="doing", priority="critical", project_id=project.id
        ))
        task_context = TaskContextService().context_for_prompt("What should I work on next for Neo?")
        context = ContextPackage(
            profile=[], preferences=[], goals=[], projects=[], relevant_memories=[], events=[], archive_results=[]
        )
        chat = object.__new__(NeoChatService)
        messages = chat.build_messages(
            "What should I work on next for Neo?", [], context,
            project_context="No project context loaded.", task_context=task_context,
        )
        self.assertIn("Ship Tasks v1", messages[0].content)
        self.assertIn("treat it as read-only", messages[0].content)
        self.assertIn("never write task details to Memory", messages[0].content)


if __name__ == "__main__":
    unittest.main()
