from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CommandCategory = Literal["read_only", "test", "build"]


class CommandRequest(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=200)
    command: list[str] = Field(min_length=1, max_length=32)
    cwd: str = "."
    category: CommandCategory
    timeout_ms: int | None = Field(default=None, ge=1000, le=300_000)
    created_by: str = Field(default="user", max_length=80)


class ApprovalRequest(BaseModel):
    confirm: bool = False
