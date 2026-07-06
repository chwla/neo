from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RepoRegisterRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    project_id: str | None = None
    name: str | None = Field(default=None, max_length=200)
    confirm: bool = False


class WorkspaceRepo(BaseModel):
    id: str
    project_id: str | None = None
    name: str
    original_path: str
    status: str
    file_count: int
    indexed_file_count: int
    total_bytes: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    deleted: bool
    created_at: str
    updated_at: str
    indexed_at: str | None = None


class RepoFile(BaseModel):
    id: str
    repo_id: str
    file_id: str
    relative_path: str
    original_relative_path: str
    language: str | None = None
    size_bytes: int
    sha256: str | None = None
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class RepoStats(BaseModel):
    file_count: int
    indexed_file_count: int
    total_bytes: int
    ignored_files: int = 0
    ignored_dirs: int = 0
    unsupported_files: int = 0
