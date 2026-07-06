from __future__ import annotations

from pydantic import BaseModel, Field


class PatchProposalRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)
    task_id: str | None = None
    project_id: str | None = None
    agent_run_id: str | None = None
    file_ids: list[str] = Field(default_factory=list, max_length=10)
