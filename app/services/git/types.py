from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class GitInitRequest(BaseModel):
    confirm: bool = False


class CheckpointCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    message: str | None = Field(default=None, max_length=1000)
    task_id: str | None = None
    agent_run_id: str | None = None
    patch_application_id: str | None = None
    test_run_id: str | None = None
    confirm: bool = False


class CheckpointRestoreRequest(BaseModel):
    confirm: bool = False


class ChangedFile(BaseModel):
    path: str
    status: str
    staged: bool = False


class GitStatus(BaseModel):
    initialized: bool
    available: bool = True
    head: str | None = None
    default_branch: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    clean: bool = True
    error: str | None = None


class GitRepoState(BaseModel):
    id: str
    repo_id: str
    project_id: str | None = None
    status: str
    git_initialized: bool
    current_head: str | None = None
    default_branch: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    initialized_at: str | None = None


class GitCheckpoint(BaseModel):
    id: str
    repo_id: str
    project_id: str | None = None
    task_id: str | None = None
    agent_run_id: str | None = None
    patch_application_id: str | None = None
    test_run_id: str | None = None
    commit_sha: str
    title: str
    message: str | None = None
    changed_files: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    status: Literal["created", "restored", "failed"] = "created"
    created_at: str


class GitOperation(BaseModel):
    id: str
    repo_id: str
    checkpoint_id: str | None = None
    operation_type: Literal["init", "status", "diff", "commit", "log", "restore"]
    status: str
    stdout_text: str = ""
    stderr_text: str = ""
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    completed_at: str | None = None


class GitDiff(BaseModel):
    repo_id: str
    path: str | None = None
    diff: str
    truncated: bool = False
