from __future__ import annotations

import hashlib
import os
import uuid

import app.services.patch_apply.store as store
from app.services.files import store as file_store
from app.services.files.extractors import extract_text
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import WorkspaceFile
from app.services.patch_apply.types import (
    PatchApplication,
    PatchApplyRequest,
    PatchValidateRequest,
    PatchValidationResult,
)
from app.services.patch_apply.validator import prepare_apply, validation_result


class ControlledPatchApplyService:
    def validate(self, artifact_id: str, request: PatchValidateRequest) -> PatchValidationResult:
        return validation_result(artifact_id, request.file_id)

    def apply(self, artifact_id: str, request: PatchApplyRequest) -> tuple[dict, dict]:
        if request.confirm is not True:
            raise ValueError("Patch application requires confirm=true.")
        try:
            prepared = prepare_apply(artifact_id, request.file_id)
        except (LookupError, ValueError) as exc:
            self._record_rejection(artifact_id, request.file_id, str(exc))
            raise
        now = file_store.now_iso()
        application_id = str(uuid.uuid4())
        application = store.insert_application(
            {
                "id": application_id,
                "artifact_id": artifact_id,
                "file_id": prepared.file["id"],
                "task_id": prepared.artifact.get("task_id"),
                "project_id": prepared.artifact.get("project_id"),
                "agent_run_id": prepared.artifact.get("agent_run_id"),
                "status": "validated",
                "original_sha256": prepared.file["sha256"],
                "new_sha256": None,
                "original_content": prepared.original_content,
                "new_content": None,
                "patch_text": prepared.parsed_patch.patch_text,
                "error": None,
                "created_at": now,
                "applied_at": None,
            }
        )

        service = WorkspaceFilesService()
        path = service.download_path(prepared.file["id"])
        temp_path = path.with_name(f".{application_id}.tmp")
        new_bytes = prepared.new_content.encode("utf-8")
        new_hash = hashlib.sha256(new_bytes).hexdigest()
        extracted, metadata = extract_text(
            prepared.file["display_name"], new_bytes, service.max_chars
        )
        try:
            temp_path.write_bytes(new_bytes)
            os.replace(temp_path, path)
            updated_file = file_store.update_file(
                prepared.file["id"],
                {
                    "sha256": new_hash,
                    "size_bytes": len(new_bytes),
                    "extracted_text": extracted,
                    "summary": None,
                    "metadata_json": {
                        **prepared.file.get("metadata", {}),
                        **metadata,
                        "last_patch_application_id": application_id,
                    },
                    "updated_at": now,
                },
            )
            if not updated_file:
                raise RuntimeError("Workspace file metadata update failed.")
            from app.services.repos.store import update_repo_file_hash

            update_repo_file_hash(prepared.file["id"], new_hash, len(new_bytes))
            application = store.update_application(
                application_id,
                {
                    "status": "applied",
                    "new_sha256": new_hash,
                    "new_content": prepared.new_content,
                    "error": None,
                    "applied_at": now,
                },
            )
        except Exception as exc:
            if temp_path.exists():
                temp_path.unlink()
            path.write_bytes(prepared.original_bytes)
            file_store.update_file(
                prepared.file["id"],
                {
                    "sha256": prepared.file["sha256"],
                    "size_bytes": prepared.file["size_bytes"],
                    "extracted_text": prepared.file.get("extracted_text"),
                    "summary": prepared.file.get("summary"),
                    "metadata_json": prepared.file.get("metadata", {}),
                },
            )
            from app.services.repos.store import update_repo_file_hash

            update_repo_file_hash(
                prepared.file["id"],
                prepared.file["sha256"],
                prepared.file["size_bytes"],
            )
            store.update_application(
                application_id,
                {
                    "status": "failed",
                    "error": str(exc),
                },
            )
            raise RuntimeError(f"Patch application failed safely: {exc}") from exc
        return application, updated_file

    @staticmethod
    def _record_rejection(artifact_id: str, requested_file_id: str | None, error: str) -> None:
        artifact = file_store.get_artifact(artifact_id)
        if not artifact:
            return
        targets = artifact.get("metadata", {}).get("target_files") or []
        target = next(
            (
                item
                for item in targets
                if not requested_file_id or item.get("file_id") == requested_file_id
            ),
            None,
        )
        if not target:
            return
        item = file_store.get_file(target.get("file_id", ""))
        if not item:
            return
        try:
            original = (
                WorkspaceFilesService().download_path(item["id"]).read_bytes().decode("utf-8")
            )
        except (LookupError, UnicodeDecodeError):
            return
        now = file_store.now_iso()
        store.insert_application(
            {
                "id": str(uuid.uuid4()),
                "artifact_id": artifact_id,
                "file_id": item["id"],
                "task_id": artifact.get("task_id"),
                "project_id": artifact.get("project_id"),
                "agent_run_id": artifact.get("agent_run_id"),
                "status": "rejected",
                "original_sha256": item["sha256"],
                "new_sha256": None,
                "original_content": original,
                "new_content": None,
                "patch_text": artifact["content"],
                "error": error,
                "created_at": now,
                "applied_at": None,
            }
        )

    @staticmethod
    def list_applications(**filters) -> list[dict]:
        return store.list_applications(**filters)

    @staticmethod
    def get_application(application_id: str) -> dict:
        item = store.get_application(application_id)
        if not item:
            raise LookupError("Patch application not found.")
        return item


def application_payload(item: dict) -> PatchApplication:
    return PatchApplication.model_validate(item)


def file_payload(item: dict) -> WorkspaceFile:
    return WorkspaceFile.model_validate(item)
