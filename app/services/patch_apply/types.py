from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PatchValidateRequest(BaseModel):
    file_id: str | None = None


class PatchApplyRequest(BaseModel):
    file_id: str | None = None
    confirm: bool = False


class PatchTargetStatus(BaseModel):
    file_id: str | None = None
    workspace_file_id: str | None = None
    repo_file_id: str | None = None
    repo_id: str | None = None
    filename: str
    relative_path: str
    change_type: Literal["modify", "create"]
    valid: bool = True
    current_sha256: str | None = None
    proposal_sha256: str | None = None
    original_size_bytes: int | None = None
    new_size_bytes: int | None = None
    errors: list[str] = Field(default_factory=list)


class PatchValidationResult(BaseModel):
    valid: bool
    target_files: list[PatchTargetStatus]
    warnings: list[str]
    errors: list[str]


class PatchApplicationFile(BaseModel):
    id: str
    patch_application_id: str
    repo_id: str | None = None
    workspace_file_id: str | None = None
    repo_file_id: str | None = None
    relative_path: str
    change_type: Literal["modify", "create"]
    status: Literal["validated", "applied", "rolled_back", "failed"]
    original_sha256: str | None = None
    new_sha256: str | None = None
    original_size_bytes: int | None = None
    new_size_bytes: int | None = None
    original_content: str | None = None
    new_content: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class PatchApplication(BaseModel):
    id: str
    artifact_id: str
    file_id: str
    task_id: str | None = None
    project_id: str | None = None
    agent_run_id: str | None = None
    status: Literal["validated", "applied", "failed", "rejected", "apply_failed_rollback_failed"]
    original_sha256: str
    new_sha256: str | None = None
    original_content: str
    new_content: str | None = None
    patch_text: str
    error: str | None = None
    created_at: str
    applied_at: str | None = None
    files: list[PatchApplicationFile] = Field(default_factory=list)
