from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

LinkType = Literal["project", "task", "note", "agent_run"]
ArtifactType = Literal["summary", "draft_file", "patch_proposal", "implementation_plan", "analysis"]


class WorkspaceFile(BaseModel):
    id: str
    filename: str
    original_filename: str
    display_name: str
    mime_type: str | None = None
    extension: str | None = None
    size_bytes: int
    sha256: str | None = None
    extracted_text: str | None = None
    summary: str | None = None
    source_type: str = "upload"
    source_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False
    created_at: str
    updated_at: str


class FileLink(BaseModel):
    id: str
    file_id: str
    link_type: LinkType
    target_id: str
    title: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class WorkspaceArtifact(BaseModel):
    id: str
    title: str
    artifact_type: ArtifactType
    content: str
    source_type: str | None = None
    source_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    note_id: str | None = None
    agent_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class FileLinkCreate(BaseModel):
    link_type: LinkType
    target_id: str = Field(min_length=1, max_length=200)
    title: str | None = Field(default=None, max_length=300)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ArtifactCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    artifact_type: ArtifactType
    content: str = Field(min_length=1)
    source_type: str | None = None
    source_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
    note_id: str | None = None
    agent_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
