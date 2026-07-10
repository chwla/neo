from __future__ import annotations

from pydantic import BaseModel, Field


class BundleExportRequest(BaseModel):
    bundle_type: str = Field(pattern="^(coding_run|agent_run|task|project)$")
    entity_id: str
    include_files: bool = True
    include_patch_text: bool = True
    include_test_output: bool = True
    redact_secrets: bool = True


class BundleImportRequest(BaseModel):
    confirm: bool
    mode: str = Field(default="archive_only", pattern="^archive_only$")
