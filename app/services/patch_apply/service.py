from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

import app.services.patch_apply.store as store
from app.services.code_index import store as code_index_store
from app.services.files import store as file_store
from app.services.files.extractors import extract_text
from app.services.files.safety import extension_for
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import FileLinkCreate, WorkspaceFile
from app.services.patch_apply.types import (
    PatchApplication,
    PatchApplyRequest,
    PatchValidateRequest,
    PatchValidationResult,
)
from app.services.patch_apply.validator import (
    PreparedApply,
    PreparedFile,
    prepare_apply,
    validation_result,
)
from app.services.repos import store as repo_store
from app.services.repos.scanner import language_for


class ControlledPatchApplyService:
    def validate(self, artifact_id: str, request: PatchValidateRequest) -> PatchValidationResult:
        return validation_result(artifact_id, request.file_id)

    def apply(
        self, artifact_id: str, request: PatchApplyRequest
    ) -> tuple[dict, dict | list[dict]]:
        if request.confirm is not True:
            raise ValueError("Patch application requires confirm=true.")
        try:
            prepared = prepare_apply(artifact_id, request.file_id)
        except (LookupError, ValueError) as exc:
            self._record_rejection(artifact_id, request.file_id, str(exc))
            raise

        application_id = str(uuid.uuid4())
        now = file_store.now_iso()
        created_records: dict[str, tuple[dict, dict]] = {}
        try:
            for item in prepared.files:
                if item.change_type == "create":
                    created_records[item.relative_path] = self._provision_created_file(
                        prepared, item, application_id, now
                    )
            primary_file = self._workspace_file(prepared.files[0], created_records)
            application = store.insert_application(
                {
                    "id": application_id,
                    "artifact_id": artifact_id,
                    "file_id": primary_file["id"],
                    "task_id": prepared.artifact.get("task_id"),
                    "project_id": prepared.artifact.get("project_id"),
                    "agent_run_id": prepared.artifact.get("agent_run_id"),
                    "status": "validated",
                    "original_sha256": prepared.files[0].target_status.current_sha256 or "",
                    "new_sha256": None,
                    "original_content": prepared.files[0].original_content,
                    "new_content": None,
                    "patch_text": prepared.parsed_patch.patch_text,
                    "error": None,
                    "created_at": now,
                    "applied_at": None,
                }
            )
            audit_ids = self._insert_audits(
                application_id, prepared.files, created_records, now
            )
        except Exception:
            self._hide_created_records(created_records)
            raise

        temp_paths: list[Path] = []
        replaced: list[PreparedFile] = []
        updated_files: list[dict] = []
        try:
            for item in prepared.files:
                item.path.parent.mkdir(parents=True, exist_ok=True)
                temp = item.path.with_name(f".{application_id}.{uuid.uuid4().hex}.tmp")
                temp.write_bytes(item.new_bytes)
                temp_paths.append(temp)
            for item, temp in zip(prepared.files, temp_paths, strict=True):
                os.replace(temp, item.path)
                replaced.append(item)
            for item in prepared.files:
                workspace_file = self._workspace_file(item, created_records)
                updated_files.append(
                    self._sync_file_metadata(
                        item, workspace_file, application_id, now, created_records
                    )
                )
            if prepared.repo:
                self._refresh_repo_metadata(prepared.repo["id"], now)
                code_index_store.mark_stale(
                    prepared.repo["id"], "Managed files changed by patch application.", now
                )
            for item in prepared.files:
                store.update_application_file(
                    audit_ids[item.relative_path],
                    {
                        "status": "applied",
                        "new_sha256": hashlib.sha256(item.new_bytes).hexdigest(),
                        "new_size_bytes": len(item.new_bytes),
                        "new_content": item.new_content,
                        "updated_at": now,
                    },
                )
            application = store.update_application(
                application_id,
                {
                    "status": "applied",
                    "new_sha256": updated_files[0]["sha256"],
                    "new_content": prepared.files[0].new_content,
                    "error": None,
                    "applied_at": now,
                },
            )
            self._attach_created_files(prepared, created_records)
        except Exception as exc:
            rollback_errors = self._rollback(
                prepared, replaced, temp_paths, created_records, audit_ids, now
            )
            status = "apply_failed_rollback_failed" if rollback_errors else "failed"
            error = str(exc)
            if rollback_errors:
                error += "; rollback failed: " + "; ".join(rollback_errors)
            store.update_application(application_id, {"status": status, "error": error})
            message = (
                "Patch application failed and rollback also failed"
                if rollback_errors
                else "Patch application failed safely and all files were rolled back"
            )
            raise RuntimeError(f"{message}: {error}") from exc
        result_files: dict | list[dict] = updated_files[0] if prepared.legacy else updated_files
        return application or store.get_application(application_id), result_files

    @staticmethod
    def _workspace_file(
        item: PreparedFile, created_records: dict[str, tuple[dict, dict]]
    ) -> dict:
        if item.workspace_file:
            return item.workspace_file
        return created_records[item.relative_path][0]

    @staticmethod
    def _provision_created_file(
        prepared: PreparedApply,
        item: PreparedFile,
        application_id: str,
        now: str,
    ) -> tuple[dict, dict]:
        if not prepared.repo:
            raise ValueError("Created files require a managed repository.")
        digest = hashlib.sha256(item.new_bytes).hexdigest()
        extracted, extraction_metadata = extract_text(
            item.relative_path,
            item.new_bytes,
            WorkspaceFilesService().max_chars,
        )
        file_id = str(uuid.uuid4())
        filename = Path(item.relative_path).name
        existing_mapping = repo_store.get_repo_file_by_path(
            prepared.repo["id"], item.relative_path
        )
        if existing_mapping:
            existing_file = file_store.get_file(
                existing_mapping["file_id"], include_deleted=True
            )
            if not existing_file or not existing_file.get("deleted"):
                raise ValueError(f"Create target metadata already exists: {item.relative_path}.")
            workspace_file = file_store.update_file(
                existing_file["id"],
                {
                    "size_bytes": len(item.new_bytes),
                    "sha256": digest,
                    "extracted_text": extracted,
                    "summary": None,
                    "deleted": False,
                    "metadata_json": {
                        **existing_file.get("metadata", {}),
                        **extraction_metadata,
                        "last_patch_application_id": application_id,
                    },
                    "updated_at": now,
                },
            )
            repo_store.update_repo_file_hash(
                existing_file["id"], digest, len(item.new_bytes)
            )
            return workspace_file, existing_mapping
        workspace_file = file_store.insert_file(
            {
                "id": file_id,
                "filename": filename,
                "original_filename": filename,
                "display_name": filename,
                "mime_type": "text/plain",
                "extension": extension_for(filename),
                "size_bytes": len(item.new_bytes),
                "sha256": digest,
                "storage_path": str(item.path),
                "extracted_text": extracted,
                "summary": None,
                "source_type": "local_repo",
                "source_id": prepared.repo["id"],
                "metadata": {
                    **extraction_metadata,
                    "source": "local_repo",
                    "repo_id": prepared.repo["id"],
                    "repo_name": prepared.repo["name"],
                    "original_path": prepared.repo["original_path"],
                    "relative_path": item.relative_path,
                    "created_by_patch": True,
                    "last_patch_application_id": application_id,
                },
                "deleted": False,
                "created_at": now,
                "updated_at": now,
            }
        )
        repo_file = repo_store.insert_repo_file(
            {
                "id": str(uuid.uuid4()),
                "repo_id": prepared.repo["id"],
                "file_id": file_id,
                "relative_path": item.relative_path,
                "original_relative_path": item.relative_path,
                "language": language_for(item.relative_path),
                "size_bytes": len(item.new_bytes),
                "sha256": digest,
                "status": "indexed",
                "metadata": {"created_by_patch": True},
                "created_at": now,
                "updated_at": now,
            }
        )
        return workspace_file, repo_file

    @staticmethod
    def _insert_audits(
        application_id: str,
        files: list[PreparedFile],
        created_records: dict[str, tuple[dict, dict]],
        now: str,
    ) -> dict[str, str]:
        ids = {}
        for item in files:
            workspace_file = item.workspace_file
            repo_file = item.repo_file
            if item.change_type == "create":
                workspace_file, repo_file = created_records[item.relative_path]
            audit_id = str(uuid.uuid4())
            store.insert_application_file(
                {
                    "id": audit_id,
                    "patch_application_id": application_id,
                    "repo_id": item.repo_id,
                    "workspace_file_id": workspace_file.get("id") if workspace_file else None,
                    "repo_file_id": repo_file.get("id") if repo_file else None,
                    "relative_path": item.relative_path,
                    "change_type": item.change_type,
                    "status": "validated",
                    "original_sha256": item.target_status.current_sha256,
                    "new_sha256": None,
                    "original_size_bytes": len(item.original_bytes),
                    "new_size_bytes": None,
                    "original_content": item.original_content,
                    "new_content": None,
                    "error": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            ids[item.relative_path] = audit_id
        return ids

    @staticmethod
    def _sync_file_metadata(
        item: PreparedFile,
        workspace_file: dict,
        application_id: str,
        now: str,
        created_records: dict[str, tuple[dict, dict]],
    ) -> dict:
        digest = hashlib.sha256(item.new_bytes).hexdigest()
        extracted, extraction_metadata = extract_text(
            item.relative_path, item.new_bytes, WorkspaceFilesService().max_chars
        )
        updated = file_store.update_file(
            workspace_file["id"],
            {
                "sha256": digest,
                "size_bytes": len(item.new_bytes),
                "extracted_text": extracted,
                "summary": None,
                "deleted": False,
                "metadata_json": {
                    **workspace_file.get("metadata", {}),
                    **extraction_metadata,
                    "last_patch_application_id": application_id,
                },
                "updated_at": now,
            },
        )
        if not updated:
            raise RuntimeError(f"Workspace file metadata update failed: {item.relative_path}.")
        repo_store.update_repo_file_hash(updated["id"], digest, len(item.new_bytes))
        return updated

    @staticmethod
    def _refresh_repo_metadata(repo_id: str, now: str) -> None:
        files, total = repo_store.list_repo_files(repo_id, limit=10000)
        repo_store.update_repo(
            repo_id,
            {
                "file_count": total,
                "indexed_file_count": total,
                "total_bytes": sum(item["size_bytes"] for item in files),
                "updated_at": now,
            },
        )

    @staticmethod
    def _attach_created_files(
        prepared: PreparedApply, created_records: dict[str, tuple[dict, dict]]
    ) -> None:
        if not prepared.repo or not prepared.repo.get("project_id"):
            return
        service = WorkspaceFilesService()
        for workspace_file, _repo_file in created_records.values():
            service.attach(
                workspace_file["id"],
                FileLinkCreate(link_type="project", target_id=prepared.repo["project_id"]),
            )

    @staticmethod
    def _hide_created_records(created_records: dict[str, tuple[dict, dict]]) -> None:
        for workspace_file, _repo_file in created_records.values():
            file_store.update_file(workspace_file["id"], {"deleted": True})

    def _rollback(
        self,
        prepared: PreparedApply,
        replaced: list[PreparedFile],
        temp_paths: list[Path],
        created_records: dict[str, tuple[dict, dict]],
        audit_ids: dict[str, str],
        now: str,
    ) -> list[str]:
        errors = []
        for temp in temp_paths:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError as exc:
                errors.append(str(exc))
        for item in reversed(replaced):
            try:
                if item.change_type == "create":
                    if item.path.exists():
                        item.path.unlink()
                else:
                    item.path.write_bytes(item.original_bytes)
                    original = item.workspace_file or {}
                    file_store.update_file(
                        original["id"],
                        {
                            "sha256": original["sha256"],
                            "size_bytes": original["size_bytes"],
                            "extracted_text": original.get("extracted_text"),
                            "summary": original.get("summary"),
                            "metadata_json": original.get("metadata", {}),
                            "updated_at": now,
                        },
                    )
                    repo_store.update_repo_file_hash(
                        original["id"], original["sha256"], original["size_bytes"]
                    )
            except Exception as exc:  # rollback must report every failed restoration
                errors.append(f"{item.relative_path}: {exc}")
        self._hide_created_records(created_records)
        for item in prepared.files:
            audit_id = audit_ids.get(item.relative_path)
            if audit_id:
                store.update_application_file(
                    audit_id,
                    {
                        "status": "failed" if errors else "rolled_back",
                        "error": "; ".join(errors) if errors else None,
                        "updated_at": now,
                    },
                )
        return errors

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
                if item.get("file_id")
                and (not requested_file_id or item.get("file_id") == requested_file_id)
            ),
            None,
        )
        if not target:
            return
        item = file_store.get_file(target["file_id"])
        if not item:
            return
        try:
            original = WorkspaceFilesService().download_path(item["id"]).read_text("utf-8")
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
