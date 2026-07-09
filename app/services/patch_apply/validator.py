from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.core.config import get_settings
from app.services.files import store as file_store
from app.services.files.service import WorkspaceFilesService
from app.services.patch_apply.applier import apply_exact_patch
from app.services.patch_apply.parser import ParsedFilePatch, ParsedPatch, parse_unified_diff
from app.services.patch_apply.types import PatchTargetStatus, PatchValidationResult
from app.services.repos import store as repo_store
from app.services.repos.safety import ensure_inside
from app.services.rules.safety import path_matches


@dataclass(frozen=True)
class PreparedFile:
    change_type: str
    relative_path: str
    repo_id: str | None
    workspace_file: dict | None
    repo_file: dict | None
    target_metadata: dict
    parsed_patch: ParsedFilePatch
    path: Path
    original_bytes: bytes
    original_content: str
    new_content: str
    new_bytes: bytes
    target_status: PatchTargetStatus


@dataclass(frozen=True)
class PreparedApply:
    artifact: dict
    repo: dict | None
    parsed_patch: ParsedPatch
    files: list[PreparedFile]
    legacy: bool


def _decode(raw: bytes) -> tuple[str, str]:
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text/code files can receive patches.") from exc
    return decoded, decoded.replace("\r\n", "\n").replace("\r", "\n")


def _metadata_targets(
    artifact: dict, parsed: ParsedPatch, file_id: str | None
) -> tuple[list[dict], bool]:
    metadata = artifact.get("metadata", {})
    if metadata.get("schema_version") == 2:
        if metadata.get("patch_kind") != "multi_file":
            raise ValueError("Patch schema version 2 requires patch_kind=multi_file.")
        targets = metadata.get("files")
        if not isinstance(targets, list) or not targets:
            raise ValueError("Multi-file patch metadata must include files.")
        if file_id:
            raise ValueError("Multi-file patches must be validated and applied atomically.")
        return targets, False

    targets = metadata.get("target_files")
    if not isinstance(targets, list) or not targets:
        raise ValueError(
            "This patch artifact does not contain enough target file metadata. "
            "Regenerate the patch proposal."
        )
    if len(parsed.files) != 1:
        raise ValueError("Multi-file diffs require patch metadata schema version 2.")
    if file_id:
        target = next((item for item in targets if item.get("file_id") == file_id), None)
        if not target:
            raise ValueError("Patch targets a file not attached to this artifact.")
    elif len(targets) == 1:
        target = targets[0]
    else:
        target = next(
            (
                item
                for item in targets
                if (item.get("relative_path") or item.get("filename")) == parsed.filename
            ),
            None,
        )
        if not target:
            raise ValueError("Choose one target file from this legacy proposal.")
    return [
        {
            "change_type": "modify",
            "relative_path": target.get("relative_path") or target.get("filename"),
            "workspace_file_id": target.get("file_id"),
            "repo_file_id": target.get("repo_file_id"),
            "repo_id": target.get("repo_id"),
            "original_sha256": target.get("sha256_at_proposal"),
            "original_size_bytes": target.get("original_size_bytes"),
        }
    ], True


def _resolve_repo(targets: list[dict], artifact: dict, *, required: bool) -> dict | None:
    repo_ids = {item.get("repo_id") for item in targets if item.get("repo_id")}
    metadata_repo = artifact.get("metadata", {}).get("repo_id")
    if metadata_repo:
        repo_ids.add(metadata_repo)
    for target in targets:
        file_id = target.get("workspace_file_id") or target.get("file_id")
        if file_id:
            mapping = repo_store.get_repo_file_by_file_id(file_id)
            if mapping:
                repo_ids.add(mapping["repo_id"])
    if not repo_ids and not required:
        return None
    if len(repo_ids) != 1:
        raise ValueError("All patch files must belong to the same managed repository.")
    repo = repo_store.get_repo(next(iter(repo_ids)))
    if not repo:
        raise LookupError("Managed repository not found.")
    return repo


def _safe_target_path(repo: dict, relative_path: str, *, create: bool) -> Path:
    root = Path(repo["workspace_path"]).resolve()
    path = ensure_inside(root, root / relative_path)
    parent = path.parent
    while parent != root:
        if parent.exists() and parent.is_symlink():
            raise ValueError("Patch targets may not traverse symlink directories.")
        parent = parent.parent
    if path.is_symlink():
        raise ValueError("Symlink targets cannot be patched.")
    if create and path.exists():
        raise ValueError(f"Create target already exists: {relative_path}.")
    if not create and not path.is_file():
        raise LookupError(f"Modify target does not exist: {relative_path}.")
    return path


def _prepare_file(repo: dict | None, target: dict, patch: ParsedFilePatch) -> PreparedFile:
    relative_path = target.get("relative_path")
    change_type = target.get("change_type")
    if change_type not in {"modify", "create"} or not relative_path:
        raise ValueError("Each patch metadata file needs change_type and relative_path.")
    if change_type != patch.change_type:
        raise ValueError(f"Metadata change type does not match diff for {relative_path}.")
    if repo:
        path = _safe_target_path(repo, relative_path, create=change_type == "create")
    elif change_type == "modify" and target.get("workspace_file_id"):
        legacy_file = file_store.get_file(target["workspace_file_id"])
        if not legacy_file:
            if file_store.get_file(target["workspace_file_id"], include_deleted=True):
                raise LookupError("Workspace target file not found or has been deleted.")
            raise LookupError("Workspace target file not found.")
        path = WorkspaceFilesService().download_path(target["workspace_file_id"])
    else:
        raise ValueError("New files require a managed repository target.")
    workspace_file = repo_file = None
    if change_type == "modify":
        file_id = target.get("workspace_file_id") or target.get("file_id")
        if not file_id or not target.get("original_sha256"):
            raise ValueError(f"Modify metadata is incomplete for {relative_path}.")
        workspace_file = file_store.get_file(file_id)
        if not workspace_file:
            raise LookupError(f"Workspace target file not found or deleted: {relative_path}.")
        repo_file = repo_store.get_repo_file_by_file_id(file_id)
        if repo and (
            not repo_file
            or repo_file["repo_id"] != repo["id"]
            or repo_file["relative_path"] != relative_path
        ):
            raise ValueError(f"Repository file metadata does not match {relative_path}.")
        expected_repo_file_id = target.get("repo_file_id")
        if expected_repo_file_id and expected_repo_file_id != repo_file["id"]:
            raise ValueError(f"Repository file id does not match {relative_path}.")
        if workspace_file["sha256"] != target["original_sha256"]:
            raise ValueError(
                f"File has changed since this patch was proposed: {relative_path}. "
                "Regenerate the patch before applying."
            )
        original_bytes = path.read_bytes()
        current_hash = hashlib.sha256(original_bytes).hexdigest()
        if current_hash != workspace_file["sha256"]:
            raise ValueError(
                f"Stored workspace file hash does not match metadata: {relative_path}."
            )
    else:
        if target.get("expected_absent") is not True:
            raise ValueError(f"Create metadata must declare expected_absent for {relative_path}.")
        existing, _ = repo_store.list_repo_files(repo["id"], q=relative_path, limit=1000)
        if any(item["relative_path"] == relative_path for item in existing):
            raise ValueError(f"Create target already exists in metadata: {relative_path}.")
        original_bytes = b""

    if b"\x00" in original_bytes[:8192]:
        raise ValueError(f"Binary workspace files cannot receive patches: {relative_path}.")
    decoded, normalized = _decode(original_bytes)
    updated = apply_exact_patch(normalized, patch)
    new_bytes = updated.encode("utf-8")
    if len(new_bytes) > get_settings().workspace_file_max_bytes:
        raise ValueError(f"Patched file exceeds the configured size limit: {relative_path}.")
    if change_type == "modify" and updated == normalized:
        raise ValueError(f"Patch does not change the current workspace file: {relative_path}.")
    status = PatchTargetStatus(
        file_id=workspace_file["id"] if workspace_file else None,
        workspace_file_id=workspace_file["id"] if workspace_file else None,
        repo_file_id=repo_file["id"] if repo_file else None,
        repo_id=repo["id"] if repo else None,
        filename=Path(relative_path).name,
        relative_path=relative_path,
        change_type=change_type,
        current_sha256=workspace_file.get("sha256") if workspace_file else None,
        proposal_sha256=target.get("original_sha256"),
        original_size_bytes=len(original_bytes),
        new_size_bytes=len(new_bytes),
    )
    return PreparedFile(
        change_type,
        relative_path,
        repo["id"] if repo else None,
        workspace_file,
        repo_file,
        target,
        patch,
        path,
        original_bytes,
        decoded,
        updated,
        new_bytes,
        status,
    )


def _prepare_components(
    artifact_id: str, file_id: str | None
) -> tuple[dict, ParsedPatch, list[dict], bool, dict | None]:
    artifact = file_store.get_artifact(artifact_id)
    if not artifact:
        raise LookupError("Patch artifact not found.")
    if artifact["artifact_type"] != "patch_proposal":
        raise ValueError("Only patch_proposal artifacts can be applied.")
    if "This patch has not been applied." not in artifact["content"]:
        raise ValueError("Patch artifact is missing its proposal-only safety marker.")
    parsed = parse_unified_diff(artifact["content"])
    targets, legacy = _metadata_targets(artifact, parsed, file_id)
    metadata_paths = [item.get("relative_path") for item in targets]
    diff_paths = [item.filename for item in parsed.files]
    if len(metadata_paths) != len(set(metadata_paths)):
        raise ValueError("Patch metadata contains duplicate target paths.")
    if set(metadata_paths) != set(diff_paths):
        raise ValueError("Patch metadata and unified diff file lists must match exactly.")
    repo = _resolve_repo(targets, artifact, required=not legacy)
    return artifact, parsed, targets, legacy, repo


def prepare_apply(artifact_id: str, file_id: str | None = None) -> PreparedApply:
    artifact, parsed, targets, legacy, repo = _prepare_components(artifact_id, file_id)
    _enforce_rule_constraints(artifact, targets)
    files = [
        _prepare_file(
            repo,
            target,
            next(item for item in parsed.files if item.filename == target["relative_path"]),
        )
        for target in targets
    ]
    return PreparedApply(artifact, repo, parsed, files, legacy)


def _enforce_rule_constraints(artifact: dict, targets: list[dict]) -> None:
    constraints = artifact.get("metadata", {}).get("rule_constraints", {})
    if not constraints:
        return
    max_files = int(constraints.get("max_files", 8))
    if len(targets) > max_files:
        raise ValueError(f"Patch exceeds resolved rule max_files ({max_files}).")
    forbidden = constraints.get("forbidden_paths", [])
    blocked = [
        item["relative_path"]
        for item in targets
        if path_matches(item.get("relative_path", ""), forbidden)
    ]
    if blocked:
        raise ValueError("Patch targets forbidden path(s): " + ", ".join(blocked))
    if not constraints.get("allow_new_files", True) and any(
        item.get("change_type") == "create" for item in targets
    ):
        raise ValueError("Resolved rules do not allow new files.")


def validation_result(artifact_id: str, file_id: str | None = None) -> PatchValidationResult:
    try:
        _artifact, parsed, targets, _legacy, repo = _prepare_components(artifact_id, file_id)
        _enforce_rule_constraints(_artifact, targets)
    except (LookupError, ValueError) as exc:
        return PatchValidationResult(valid=False, target_files=[], warnings=[], errors=[str(exc)])
    statuses, errors = [], []
    for target in targets:
        relative_path = target["relative_path"]
        patch = next(item for item in parsed.files if item.filename == relative_path)
        try:
            statuses.append(_prepare_file(repo, target, patch).target_status)
        except (LookupError, ValueError) as exc:
            message = str(exc)
            errors.append(message)
            statuses.append(
                PatchTargetStatus(
                    file_id=target.get("workspace_file_id") or target.get("file_id"),
                    workspace_file_id=target.get("workspace_file_id") or target.get("file_id"),
                    repo_file_id=target.get("repo_file_id"),
                    repo_id=target.get("repo_id") or (repo.get("id") if repo else None),
                    filename=Path(relative_path).name,
                    relative_path=relative_path,
                    change_type=target.get("change_type", "modify"),
                    valid=False,
                    current_sha256=None,
                    proposal_sha256=target.get("original_sha256"),
                    original_size_bytes=target.get("original_size_bytes"),
                    errors=[message],
                )
            )
    return PatchValidationResult(
        valid=not errors,
        target_files=statuses,
        warnings=[
            "Applying is atomic and affects only Neo's managed workspace copy. "
            "No tests or checkpoints run automatically."
        ],
        errors=errors,
    )
