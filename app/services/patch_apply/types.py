from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class PatchValidateRequest(BaseModel):
    file_id: str | None = None


class PatchApplyRequest(BaseModel):
    file_id: str | None = None
    confirm: bool = False


class PatchTargetStatus(BaseModel):
    file_id: str
    filename: str
    current_sha256: str
    proposal_sha256: str


class PatchValidationResult(BaseModel):
    valid: bool
    target_files: list[PatchTargetStatus]
    warnings: list[str]
    errors: list[str]


class PatchApplication(BaseModel):
    id: str
    artifact_id: str
    file_id: str
    task_id: str | None = None
    project_id: str | None = None
    agent_run_id: str | None = None
    status: Literal["validated", "applied", "failed", "rejected"]
    original_sha256: str
    new_sha256: str | None = None
    original_content: str
    new_content: str | None = None
    patch_text: str
    error: str | None = None
    created_at: str
    applied_at: str | None = None
