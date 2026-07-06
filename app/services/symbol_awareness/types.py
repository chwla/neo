from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SymbolAwarenessBuildRequest(BaseModel):
    force: bool = False


class CodeReference(BaseModel):
    id: str
    repo_id: str
    symbol_id: str | None = None
    referenced_name: str
    reference_type: str
    source_repo_file_id: str
    source_file_id: str
    source_relative_path: str
    line_start: int | None = None
    line_end: int | None = None
    column_start: int | None = None
    column_end: int | None = None
    context_text: str | None = None
    resolved: bool
    confidence: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str


class SymbolRelationship(BaseModel):
    id: str
    repo_id: str
    source_symbol_id: str
    target_symbol_id: str
    relationship_type: str
    confidence: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RelatedFile(BaseModel):
    id: str
    repo_id: str
    source_repo_file_id: str
    target_repo_file_id: str
    relationship_type: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
