from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.services.files import store as file_store
from app.services.files.service import WorkspaceFilesService
from app.services.patch_apply.applier import apply_exact_patch
from app.services.patch_apply.parser import ParsedPatch, parse_unified_diff
from app.services.patch_apply.types import PatchTargetStatus, PatchValidationResult


@dataclass(frozen=True)
class PreparedApply:
    artifact: dict
    file: dict
    target_metadata: dict
    parsed_patch: ParsedPatch
    original_bytes: bytes
    original_content: str
    new_content: str
    target_status: PatchTargetStatus


def prepare_apply(artifact_id: str, file_id: str | None = None) -> PreparedApply:
    artifact = file_store.get_artifact(artifact_id)
    if not artifact:
        raise LookupError("Patch artifact not found.")
    if artifact["artifact_type"] != "patch_proposal":
        raise ValueError("Only patch_proposal artifacts can be applied.")
    if "This patch has not been applied." not in artifact["content"]:
        raise ValueError("Patch artifact is missing its proposal-only safety marker.")
    targets = artifact.get("metadata", {}).get("target_files")
    if not isinstance(targets, list) or not targets:
        raise ValueError(
            "This patch artifact does not contain enough target file metadata. "
            "Regenerate the patch proposal."
        )
    if file_id:
        target = next((item for item in targets if item.get("file_id") == file_id), None)
        if not target:
            raise ValueError("Patch targets a file not attached to this artifact.")
    elif len(targets) == 1:
        target = targets[0]
        file_id = target.get("file_id")
    else:
        raise ValueError("Choose one target file from this multi-file proposal.")
    if not file_id or not target.get("filename") or not target.get("sha256_at_proposal"):
        raise ValueError(
            "This patch artifact does not contain enough target file metadata. "
            "Regenerate the patch proposal."
        )

    item = file_store.get_file(file_id)
    if not item:
        raise LookupError("Workspace target file not found or has been deleted.")
    if item["display_name"] != target["filename"]:
        raise ValueError("Patch target filename does not match workspace file metadata.")
    if item["sha256"] != target["sha256_at_proposal"]:
        raise ValueError(
            "File has changed since this patch was proposed. Regenerate the patch before applying."
        )

    service = WorkspaceFilesService()
    raw = service.download_path(file_id).read_bytes()
    current_hash = hashlib.sha256(raw).hexdigest()
    if current_hash != item["sha256"]:
        raise ValueError("Stored workspace file hash does not match its metadata.")
    if b"\x00" in raw[:8192]:
        raise ValueError("Binary workspace files cannot receive text patches.")
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1")
    original = decoded.replace("\r\n", "\n").replace("\r", "\n")
    parsed = parse_unified_diff(artifact["content"])
    expected_path = target.get("relative_path") or item["display_name"]
    if parsed.filename != expected_path:
        raise ValueError("Unified diff target does not match the selected workspace file.")
    updated = apply_exact_patch(original, parsed)
    if updated == original:
        raise ValueError("Patch does not change the current workspace file.")
    target_status = PatchTargetStatus(
        file_id=file_id,
        filename=item["display_name"],
        current_sha256=item["sha256"],
        proposal_sha256=target["sha256_at_proposal"],
    )
    return PreparedApply(artifact, item, target, parsed, raw, decoded, updated, target_status)


def validation_result(artifact_id: str, file_id: str | None = None) -> PatchValidationResult:
    try:
        prepared = prepare_apply(artifact_id, file_id)
    except (LookupError, ValueError) as exc:
        return PatchValidationResult(valid=False, target_files=[], warnings=[], errors=[str(exc)])
    return PatchValidationResult(
        valid=True,
        target_files=[prepared.target_status],
        warnings=[
            "Applying will modify only Neo's managed workspace copy. No tests or code will run."
        ],
        errors=[],
    )
