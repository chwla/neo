from __future__ import annotations

from pydantic import BaseModel

from app.services.memory.queries import RecallResult


class ContextPackage(BaseModel):
    profile: list
    preferences: list
    goals: list
    projects: list
    relevant_memories: list
    events: list
    archive_results: list
    canonical_recall: RecallResult | None = None
