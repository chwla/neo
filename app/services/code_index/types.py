from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CodeIndexBuildRequest(BaseModel):
    force: bool = False
    summarize: bool = True


class CodeIndex(BaseModel):
    id: str
    repo_id: str
    status: str
    file_count: int
    indexed_file_count: int
    symbol_count: int
    dependency_count: int
    route_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
    indexed_at: str | None = None


class CodeSymbol(BaseModel):
    id: str
    repo_id: str
    repo_file_id: str
    file_id: str
    relative_path: str
    name: str
    qualified_name: str | None = None
    symbol_type: str
    language: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    signature: str | None = None
    parent_symbol_id: str | None = None
    doc_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class CodeDependency(BaseModel):
    id: str
    repo_id: str
    source_repo_file_id: str
    target_repo_file_id: str | None = None
    source_relative_path: str
    target_relative_path: str | None = None
    import_text: str
    dependency_type: str
    resolved: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class CodeFileSummary(BaseModel):
    id: str
    repo_id: str
    repo_file_id: str
    file_id: str
    relative_path: str
    language: str | None = None
    summary: str
    purpose: str | None = None
    key_symbols: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
