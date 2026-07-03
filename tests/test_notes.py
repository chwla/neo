import os
import tempfile
import unittest

from app.core.config import get_settings
from app.services.notes import NoteCreate, NoteUpdate, NotesService
from app.services.notes.store import initialize_notes_tables
from app.services.research.types import DepthMode, JobStatus, ResearchJob


class NotesServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmpdir.name}/notes.db"
        get_settings.cache_clear()
        initialize_notes_tables()
        self.service = NotesService()

    def tearDown(self) -> None:
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        self.tmpdir.cleanup()

    def test_crud_tags_search_pin_archive_delete(self):
        note = self.service.create_note(
            NoteCreate(
                title="",
                body="First line\nBody mentions durable notes.",
                tags=[" Research ", "research", "Ideas"],
            )
        )

        self.assertEqual(note.title, "First line")
        self.assertEqual(set(note.tags), {"ideas", "research"})

        notes, total = self.service.list_notes(q="durable")
        self.assertEqual(total, 1)
        self.assertEqual(notes[0].id, note.id)

        notes, total = self.service.list_notes(tag="research")
        self.assertEqual(total, 1)
        self.assertEqual(notes[0].id, note.id)

        updated = self.service.update_note(
            note.id,
            NoteUpdate(title="Updated", body="Updated body", tags=["done"]),
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.title, "Updated")
        self.assertEqual(updated.tags, ["done"])

        pinned = self.service.set_pinned(note.id, True)
        self.assertTrue(pinned.pinned)

        archived = self.service.set_archived(note.id, True)
        self.assertTrue(archived.archived)

        visible, visible_total = self.service.list_notes()
        self.assertEqual(visible, [])
        self.assertEqual(visible_total, 0)

        archived_notes, archived_total = self.service.list_notes(include_archived=True)
        self.assertEqual(archived_total, 1)
        self.assertEqual(archived_notes[0].id, note.id)

        self.assertTrue(self.service.soft_delete(note.id))
        deleted, deleted_total = self.service.list_notes(include_archived=True)
        self.assertEqual(deleted, [])
        self.assertEqual(deleted_total, 0)

    def test_save_research_report_does_not_duplicate(self):
        job = ResearchJob(
            id="job-1",
            user_query="Research Neo notes",
            depth=DepthMode.STANDARD,
            status=JobStatus.COMPLETED,
            created_at="2026-06-24T00:00:00+00:00",
            updated_at="2026-06-24T00:10:00+00:00",
            report="# Research Neo notes\n\nA complete report.",
            metadata={"confidence": "medium", "report_type": "research"},
        )

        first = self.service.save_research_report(job, title=None, tags=["Saved"])
        second = self.service.save_research_report(job, title=None, tags=["Saved"])

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.source_type, "research_report")
        self.assertEqual(first.source_id, "job-1")
        self.assertEqual(set(first.tags), {"research", "saved"})
        self.assertEqual(first.source_metadata["mode"], "standard")


if __name__ == "__main__":
    unittest.main()
