from __future__ import annotations

from pydantic import BaseModel, Field


class ConnectionCreate(BaseModel):
    name: str
    owner: str
    repo: str
    token_ref: str = Field(default="GITHUB_TOKEN", pattern=r"^[A-Z][A-Z0-9_]*$")
    enabled: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = None
    owner: str | None = None
    repo: str | None = None
    token_ref: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    enabled: bool | None = None


class PRDraftRequest(BaseModel):
    confirm: bool
    title: str
    body: str = ""
    head_branch: str
    base_branch: str = "main"
    checkpoint_id: str | None = None
    coding_run_id: str | None = None
