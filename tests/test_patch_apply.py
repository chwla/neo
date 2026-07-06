import hashlib
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app
from app.services.files import store as file_store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import ArtifactCreate
from app.services.patch_apply import (
    ControlledPatchApplyService,
    PatchApplyRequest,
    PatchValidateRequest,
)
from app.services.patch_apply import store as apply_store
from app.services.patches import PatchProposalRequest, PatchProposalService

VALID_PROPOSAL = """# Patch Proposal

## Objective
Use an f-string.

## Target files
- one.py

## Summary
Modernize formatting.

## Proposed changes
Replace concatenation.

## Unified diff
```diff
diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
@@ -1,2 +1,2 @@
 def greeting(name):
-    return "Hello " + name
+    return f"Hello {name}"
```

## Risks
Review the output.

## Validation needed
Manual review.

## Notes
This patch has not been applied."""


class ControlledPatchApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.tmp.name}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = f"{self.tmp.name}/files"
        get_settings.cache_clear()
        file_store.initialize_workspace_file_tables()
        self.files = WorkspaceFilesService()
        self.item = self.files.import_bytes(
            original_filename="one.py",
            content=b'def greeting(name):\n    return "Hello " + name\n',
            mime_type="text/x-python",
        )
        self.artifact = PatchProposalService(generator=lambda _prompt: VALID_PROPOSAL).propose(
            PatchProposalRequest(objective="Use an f-string", file_ids=[self.item["id"]])
        )
        self.service = ControlledPatchApplyService()

    def tearDown(self):
        get_settings.cache_clear()
        os.environ.pop("NEO_DATABASE_URL", None)
        os.environ.pop("NEO_WORKSPACE_FILES_DIR", None)
        self.tmp.cleanup()

    def artifact_with(self, content: str, *, artifact_type="patch_proposal", metadata=None):
        return self.files.create_artifact(
            ArtifactCreate(
                title="Unsafe test proposal",
                artifact_type=artifact_type,
                content=content,
                source_type="patch_proposal",
                metadata=metadata if metadata is not None else self.artifact["metadata"],
            )
        )

    def test_validate_is_read_only_and_apply_updates_file_with_snapshot(self):
        original_path = self.files.download_path(self.item["id"])
        original_bytes = original_path.read_bytes()
        before = file_store.get_file(self.item["id"])

        validation = self.service.validate(self.artifact["id"], PatchValidateRequest())
        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(validation.target_files[0].current_sha256, before["sha256"])
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(apply_store.list_applications(), [])

        application, updated = self.service.apply(
            self.artifact["id"], PatchApplyRequest(confirm=True)
        )
        expected = b'def greeting(name):\n    return f"Hello {name}"\n'
        self.assertEqual(original_path.read_bytes(), expected)
        self.assertEqual(updated["extracted_text"], expected.decode())
        self.assertEqual(updated["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertNotEqual(updated["sha256"], before["sha256"])
        self.assertEqual(application["status"], "applied")
        self.assertEqual(application["original_content"], original_bytes.decode())
        self.assertEqual(application["new_content"], expected.decode())
        self.assertEqual(application["original_sha256"], before["sha256"])
        self.assertEqual(application["new_sha256"], updated["sha256"])

        file_store.initialize_workspace_file_tables()
        persisted = ControlledPatchApplyService.get_application(application["id"])
        self.assertEqual(persisted["new_content"], expected.decode())
        self.assertEqual(file_store.get_file(self.item["id"])["sha256"], updated["sha256"])

    def test_apply_requires_confirmation_and_stale_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            self.service.apply(self.artifact["id"], PatchApplyRequest(confirm=False))

        path = self.files.download_path(self.item["id"])
        changed = b'def greeting(name):\n    return "Hi " + name\n'
        path.write_bytes(changed)
        file_store.update_file(
            self.item["id"],
            {
                "sha256": hashlib.sha256(changed).hexdigest(),
                "size_bytes": len(changed),
                "extracted_text": changed.decode(),
            },
        )
        result = self.service.validate(self.artifact["id"], PatchValidateRequest())
        self.assertFalse(result.valid)
        self.assertIn("changed since this patch was proposed", result.errors[0])

    def test_rejects_missing_non_patch_deleted_unknown_and_hashless_artifacts(self):
        self.assertFalse(self.service.validate("missing", PatchValidateRequest()).valid)
        analysis = self.artifact_with("Analysis only", artifact_type="analysis")
        self.assertIn(
            "Only patch_proposal",
            self.service.validate(analysis["id"], PatchValidateRequest()).errors[0],
        )
        hashless = self.artifact_with(VALID_PROPOSAL, metadata={})
        self.assertIn(
            "not contain enough target file metadata",
            self.service.validate(hashless["id"], PatchValidateRequest()).errors[0],
        )
        unknown = self.artifact_with(
            VALID_PROPOSAL,
            metadata={
                "target_files": [
                    {
                        "file_id": "missing",
                        "filename": "one.py",
                        "sha256_at_proposal": "abc",
                    }
                ]
            },
        )
        self.assertIn(
            "not found",
            self.service.validate(unknown["id"], PatchValidateRequest()).errors[0],
        )
        file_store.update_file(self.item["id"], {"deleted": True})
        self.assertIn(
            "deleted",
            self.service.validate(self.artifact["id"], PatchValidateRequest()).errors[0],
        )

    def test_rejects_unsafe_paths_operations_multiple_files_and_bad_context(self):
        replacements = {
            "absolute path": VALID_PROPOSAL.replace("a/one.py", "/tmp/one.py", 1),
            "path traversal": VALID_PROPOSAL.replace("a/one.py", "a/../one.py", 1),
            "new file": VALID_PROPOSAL.replace(
                "--- a/one.py", "new file mode 100644\n--- /dev/null"
            ),
            "deleted file": VALID_PROPOSAL.replace(
                "--- a/one.py", "deleted file mode 100644\n--- a/one.py"
            ),
            "rename": VALID_PROPOSAL.replace(
                "--- a/one.py", "rename from one.py\nrename to two.py\n--- a/one.py"
            ),
            "bad context": VALID_PROPOSAL.replace("def greeting(name):", "def missing(name):"),
            "unknown filename": VALID_PROPOSAL.replace("one.py", "other.py"),
            "multiple files": VALID_PROPOSAL.replace(
                "```\n\n## Risks",
                "diff --git a/two.py b/two.py\n--- a/two.py\n+++ b/two.py\n"
                "@@ -1 +1 @@\n-old\n+new\n```\n\n## Risks",
            ),
        }
        for label, content in replacements.items():
            with self.subTest(label=label):
                artifact = self.artifact_with(content)
                result = self.service.validate(artifact["id"], PatchValidateRequest())
                self.assertFalse(result.valid, label)

    def test_endpoints_confirmation_history_read_and_artifact_download(self):
        client = TestClient(create_app())
        validate = client.post(f"/api/patches/{self.artifact['id']}/validate-apply", json={})
        self.assertEqual(validate.status_code, 200)
        self.assertTrue(validate.json()["valid"])
        denied = client.post(f"/api/patches/{self.artifact['id']}/apply", json={"confirm": False})
        self.assertEqual(denied.status_code, 400)
        applied = client.post(f"/api/patches/{self.artifact['id']}/apply", json={"confirm": True})
        self.assertEqual(applied.status_code, 200, applied.text)
        application_id = applied.json()["application"]["id"]
        history = client.get("/api/patches/applications", params={"file_id": self.item["id"]})
        self.assertEqual(len(history.json()["applications"]), 1)
        detail = client.get(f"/api/patches/applications/{application_id}")
        self.assertIn("Hello ", detail.json()["application"]["original_content"])
        original_download = client.get(
            f"/api/patches/applications/{application_id}/download",
            params={"version": "original"},
        )
        current_download = client.get(
            f"/api/patches/applications/{application_id}/download",
            params={"version": "current"},
        )
        self.assertIn('return "Hello " + name', original_download.text)
        self.assertIn('return f"Hello {name}"', current_download.text)
        download = client.get(f"/api/artifacts/{self.artifact['id']}/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn("diff --git", download.text)


if __name__ == "__main__":
    unittest.main()
