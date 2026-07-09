import os
import tempfile
import unittest

from app.core.config import get_settings
from app.services.notes import NoteCreate, NotesService
from app.services.notes.store import initialize_notes_tables
from app.services.projects import ProjectContextService, ProjectCreate, ProjectsService
from app.services.projects.store import initialize_project_tables
from app.services.projects.types import ProjectUpdate


class ProjectsServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmpdir.name}/projects.db"
        get_settings.cache_clear()
        initialize_notes_tables()
        initialize_project_tables()
        self.notes = NotesService()
        self.projects = ProjectsService()

    def tearDown(self) -> None:
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        self.tmpdir.cleanup()

    def test_crud_search_tags_pin_archive_delete_and_persistence(self):
        project = self.projects.create_project(
            ProjectCreate(
                title="Neo",
                description="Self-hosted personal AI assistant",
                priority="high",
                tags=["AI", "personal-os", "ai"],
            )
        )

        self.assertEqual(project.title, "Neo")
        self.assertEqual(project.status, "active")
        self.assertEqual(project.priority, "high")
        self.assertEqual(project.tags, ["ai", "personal-os"])

        projects, total = self.projects.list_projects(q="self-hosted")
        self.assertEqual(total, 1)
        self.assertEqual(projects[0].id, project.id)

        projects, total = self.projects.list_projects(tag="AI")
        self.assertEqual(total, 1)
        self.assertEqual(projects[0].id, project.id)

        tags = self.projects.list_tags()
        self.assertEqual(tags[0].tag, "ai")
        self.assertEqual(tags[0].count, 1)

        updated = self.projects.update_project(
            project.id,
            ProjectUpdate(
                title="Neo Workspace",
                description="Updated",
                status="paused",
                priority="critical",
                tags=["workspace"],
            ),
        )
        self.assertEqual(updated.title, "Neo Workspace")
        self.assertEqual(updated.status, "paused")
        self.assertEqual(updated.priority, "critical")
        self.assertEqual(updated.tags, ["workspace"])

        pinned = self.projects.set_pinned(project.id, True)
        self.assertTrue(pinned.pinned)

        archived = self.projects.set_archived(project.id, True)
        self.assertTrue(archived.archived)
        self.assertEqual(archived.status, "archived")

        visible, visible_total = self.projects.list_projects()
        self.assertEqual(visible, [])
        self.assertEqual(visible_total, 0)

        archived_projects, archived_total = self.projects.list_projects(include_archived=True)
        self.assertEqual(archived_total, 1)
        self.assertEqual(archived_projects[0].id, project.id)

        self.assertIsNotNone(ProjectsService().get_project(project.id))
        self.assertTrue(self.projects.soft_delete(project.id))
        deleted, deleted_total = self.projects.list_projects(include_archived=True)
        self.assertEqual(deleted, [])
        self.assertEqual(deleted_total, 0)

    def test_attach_detach_note_is_idempotent_and_read_includes_notes(self):
        project = self.projects.create_project(ProjectCreate(title="Shelfd", tags=["product"]))
        note = self.notes.create_note(
            NoteCreate(
                title="Shelfd idea",
                body="Build a lightweight reading shelf.",
                tags=["idea"],
            )
        )

        self.assertTrue(self.projects.attach_note(project.id, note.id))
        self.assertTrue(self.projects.attach_note(project.id, note.id))

        notes = self.projects.list_project_notes(project.id)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].title, "Shelfd idea")

        read = self.projects.read_project(project.id)
        self.assertIsNotNone(read)
        _, read_notes, links = read
        self.assertEqual(len(read_notes), 1)
        self.assertEqual(links, [])

        linked_projects = self.projects.list_note_projects(note.id)
        self.assertEqual(len(linked_projects), 1)
        self.assertEqual(linked_projects[0].title, "Shelfd")

        self.assertTrue(self.projects.detach_note(project.id, note.id))
        self.assertEqual(self.projects.list_project_notes(project.id), [])

    def test_project_context_only_loads_when_project_is_mentioned(self):
        project = self.projects.create_project(
            ProjectCreate(
                title="Neo",
                description="Personal assistant project",
                priority="high",
            )
        )
        note = self.notes.create_note(
            NoteCreate(title="Neo roadmap", body="Ship Projects v1 and Notes links.")
        )
        self.projects.attach_note(project.id, note.id)

        unrelated = ProjectContextService().context_for_prompt("What is the weather?")
        self.assertEqual(unrelated, "No project context loaded.")

        context = ProjectContextService().context_for_prompt("What is the status of Neo?")
        self.assertIn("Project: Neo", context)
        self.assertIn("Priority: high", context)
        self.assertIn("Neo roadmap", context)


if __name__ == "__main__":
    unittest.main()
