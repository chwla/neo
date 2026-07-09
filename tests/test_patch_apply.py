import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from app.services.repos import store as repo_store

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
        self.assertEqual(len(detail.json()["application"]["files"]), 1)
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


class MultiFilePatchApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        os.environ["NEO_DATABASE_URL"] = f"sqlite:///{self.root}/neo.db"
        os.environ["NEO_WORKSPACE_FILES_DIR"] = str(self.root / "files")
        os.environ["NEO_WORKSPACE_REPOS_DIR"] = str(self.root / "repos")
        get_settings.cache_clear()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text('VALUE = "old"\n')
        (self.source / "config.py").write_text('FLAG = False\n')
        self.original = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.source.iterdir()
        }
        self.client = TestClient(create_app())
        response = self.client.post(
            "/api/repos/register", json={"path": str(self.source), "confirm": True}
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.repo = response.json()["repo"]
        self.managed = Path(repo_store.get_repo(self.repo["id"])["workspace_path"])
        mappings, _ = repo_store.list_repo_files(self.repo["id"], limit=100)
        self.mappings = {item["relative_path"]: item for item in mappings}
        self.files = WorkspaceFilesService()
        self.service = ControlledPatchApplyService()

    def tearDown(self):
        get_settings.cache_clear()
        for name in (
            "NEO_DATABASE_URL",
            "NEO_WORKSPACE_FILES_DIR",
            "NEO_WORKSPACE_REPOS_DIR",
        ):
            os.environ.pop(name, None)
        self.tmp.cleanup()

    def target(self, path):
        mapping = self.mappings[path]
        item = file_store.get_file(mapping["file_id"])
        return {
            "change_type": "modify",
            "relative_path": path,
            "workspace_file_id": item["id"],
            "repo_file_id": mapping["id"],
            "original_sha256": item["sha256"],
            "original_size_bytes": item["size_bytes"],
        }

    def artifact(self, content, files):
        targets = []
        for item in files:
            targets.append(
                {
                    **item,
                    "file_id": item.get("workspace_file_id"),
                    "filename": Path(item["relative_path"]).name,
                    "repo_id": self.repo["id"],
                    "sha256_at_proposal": item.get("original_sha256"),
                }
            )
        return self.files.create_artifact(
            ArtifactCreate(
                title="Multi-file patch",
                artifact_type="patch_proposal",
                content=content,
                source_type="patch_proposal",
                metadata={
                    "schema_version": 2,
                    "patch_kind": "multi_file",
                    "repo_id": self.repo["id"],
                    "files": files,
                    "target_files": targets,
                    "proposal_only": True,
                },
            )
        )

    @staticmethod
    def proposal(diff):
        return f"""# Patch Proposal

## Unified diff
```diff
{diff}
```

## Notes
This patch has not been applied."""

    def modify_two_artifact(self):
        diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = "old"
+VALUE = "new"
diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1 +1 @@
-FLAG = False
+FLAG = True"""
        return self.artifact(
            self.proposal(diff), [self.target("app.py"), self.target("config.py")]
        )

    def modify_create_artifact(self):
        created = {
            "change_type": "create",
            "relative_path": "tests/test_extra.py",
            "expected_absent": True,
        }
        diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = "old"
+VALUE = "new"
diff --git a/tests/test_extra.py b/tests/test_extra.py
new file mode 100644
--- /dev/null
+++ b/tests/test_extra.py
@@ -0,0 +1,2 @@
+def test_value():
+    assert True"""
        return self.artifact(self.proposal(diff), [self.target("app.py"), created])

    def assert_original_unchanged(self):
        for path in self.source.iterdir():
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), self.original[path.name]
            )

    def test_validate_and_apply_two_existing_files_atomically_with_audit(self):
        artifact = self.modify_two_artifact()
        validation = self.service.validate(artifact["id"], PatchValidateRequest())
        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(
            [item.relative_path for item in validation.target_files], ["app.py", "config.py"]
        )
        application, updated = self.service.apply(
            artifact["id"], PatchApplyRequest(confirm=True)
        )
        self.assertEqual(len(updated), 2)
        self.assertEqual((self.managed / "app.py").read_text(), 'VALUE = "new"\n')
        self.assertEqual((self.managed / "config.py").read_text(), "FLAG = True\n")
        self.assertEqual(
            {item["relative_path"] for item in application["files"]}, {"app.py", "config.py"}
        )
        self.assertTrue(all(item["status"] == "applied" for item in application["files"]))
        self.assert_original_unchanged()

    def test_proposal_service_generates_schema_v2_metadata(self):
        proposal = self.modify_create_artifact()["content"]
        artifact = PatchProposalService(generator=lambda _prompt: proposal).propose(
            PatchProposalRequest(
                objective="Update app and add a test helper",
                file_ids=[
                    self.mappings["app.py"]["file_id"],
                    self.mappings["config.py"]["file_id"],
                ],
            )
        )
        self.assertEqual(artifact["artifact_type"], "patch_proposal")
        self.assertEqual(artifact["metadata"]["schema_version"], 2)
        self.assertEqual(artifact["metadata"]["patch_kind"], "multi_file")
        self.assertEqual(
            [
                (item["relative_path"], item["change_type"])
                for item in artifact["metadata"]["files"]
            ],
            [("app.py", "modify"), ("tests/test_extra.py", "create")],
        )

    def test_modify_and_create_syncs_metadata_and_download_bundle(self):
        artifact = self.modify_create_artifact()
        application, _updated = self.service.apply(
            artifact["id"], PatchApplyRequest(confirm=True)
        )
        created_path = self.managed / "tests/test_extra.py"
        self.assertTrue(created_path.is_file())
        mappings, _ = repo_store.list_repo_files(self.repo["id"], q="test_extra.py")
        self.assertEqual(mappings[0]["relative_path"], "tests/test_extra.py")
        created_file = file_store.get_file(mappings[0]["file_id"])
        self.assertIn("def test_value", created_file["extracted_text"])
        detail = self.client.get(f"/api/patches/applications/{application['id']}")
        self.assertEqual(len(detail.json()["application"]["files"]), 2)
        bundle = self.client.get(f"/api/patches/applications/{application['id']}/download")
        self.assertIn("tests/test_extra.py", bundle.text)
        self.assertIn("patch_text", bundle.text)
        self.assert_original_unchanged()

    def test_stale_one_file_rejects_whole_patch(self):
        artifact = self.modify_two_artifact()
        path = self.managed / "config.py"
        before = (self.managed / "app.py").read_bytes()
        changed = b"FLAG = None\n"
        path.write_bytes(changed)
        mapping = self.mappings["config.py"]
        file_store.update_file(
            mapping["file_id"],
            {"sha256": hashlib.sha256(changed).hexdigest(), "size_bytes": len(changed)},
        )
        result = self.service.validate(artifact["id"], PatchValidateRequest())
        self.assertFalse(result.valid)
        self.assertEqual((self.managed / "app.py").read_bytes(), before)

    def test_rollback_restores_modified_and_removes_created(self):
        artifact = self.modify_create_artifact()
        real_replace = os.replace
        calls = 0

        def fail_second(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected replace failure")
            return real_replace(source, destination)

        with patch("app.services.patch_apply.service.os.replace", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                self.service.apply(artifact["id"], PatchApplyRequest(confirm=True))
        self.assertEqual((self.managed / "app.py").read_text(), 'VALUE = "old"\n')
        self.assertFalse((self.managed / "tests/test_extra.py").exists())
        applications = apply_store.list_applications(artifact_id=artifact["id"])
        self.assertEqual(applications[0]["status"], "failed")
        self.assertTrue(
            all(item["status"] == "rolled_back" for item in applications[0]["files"])
        )
        retried, _updated = self.service.apply(
            artifact["id"], PatchApplyRequest(confirm=True)
        )
        self.assertEqual(retried["status"], "applied")
        self.assertTrue((self.managed / "tests/test_extra.py").is_file())
        self.assert_original_unchanged()

    def test_rejects_unsafe_multifile_operations(self):
        base = self.modify_two_artifact()
        content = base["content"]
        cases = {
            "duplicate": content.replace(
                "diff --git a/config.py b/config.py", "diff --git a/app.py b/app.py"
            ).replace("--- a/config.py", "--- a/app.py").replace(
                "+++ b/config.py", "+++ b/app.py"
            ),
            "git": content.replace("config.py", ".git/config"),
            "delete": content.replace(
                "--- a/config.py", "deleted file mode 100644\n--- a/config.py"
            ),
            "rename": content.replace(
                "--- a/config.py",
                "rename from config.py\nrename to other.py\n--- a/config.py",
            ),
            "binary": content.replace("--- a/config.py", "GIT binary patch\n--- a/config.py"),
            "permission": content.replace(
                "--- a/config.py", "old mode 100644\nnew mode 100755\n--- a/config.py"
            ),
        }
        for label, unsafe in cases.items():
            with self.subTest(label=label):
                artifact = self.artifact(
                    unsafe, [self.target("app.py"), self.target("config.py")]
                )
                self.assertFalse(
                    self.service.validate(artifact["id"], PatchValidateRequest()).valid
                )


if __name__ == "__main__":
    unittest.main()
