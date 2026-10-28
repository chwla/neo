from __future__ import annotations

from pydantic import BaseModel, SecretStr


class ConnectionCreate(BaseModel):
    name: str
    owner: str
    repo: str
    token: SecretStr
    enabled: bool = True


class ConnectionUpdate(BaseModel):
    name: str | None = None
    owner: str | None = None
    repo: str | None = None
    token: SecretStr | None = None
    enabled: bool | None = None


class PRDraftRequest(BaseModel):
    confirm: bool
    title: str
    body: str = ""
    head_branch: str
    base_branch: str = "main"
    checkpoint_id: str | None = None
    coding_run_id: str | None = None
