from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import GoalStatus, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.schemas.memory_objects import (
    EventRead,
    GoalRead,
    MemoryRead,
    PreferenceRead,
    ProfileFactRead,
    ProjectRead,
)
from app.services.archives import ArchiveSearchResult, QdrantArchiveService


class RetrievalRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=8, ge=1, le=50)
    include_archives: bool = True


class RetrievalResult(BaseModel):
    profile: list[ProfileFactRead]
    preferences: list[PreferenceRead]
    goals: list[GoalRead]
    projects: list[ProjectRead]
    relevant_memories: list[MemoryRead]
    events: list[EventRead]
    archive_results: list[ArchiveSearchResult]
    archive_error: str | None = None


class RetrievalService:
    """Retrieve structured memory in priority order plus optional archive hits."""

    def __init__(
        self,
        archive_service: QdrantArchiveService | None = None,
        enable_default_archives: bool = False,
    ) -> None:
        self.archive_service = archive_service
        self.enable_default_archives = enable_default_archives

    def retrieve(self, store: MemoryStore, request: RetrievalRequest) -> RetrievalResult:
        memories = store.search_memories(request.query, limit=request.limit)
        for memory in memories:
            memory.last_accessed_at = datetime.now(UTC)

        archive_results: list[ArchiveSearchResult] = []
        archive_error = None
        if request.include_archives:
            try:
                archive_results = self._archives().search(request.query, limit=request.limit)
            except Exception as exc:  # Qdrant can be unavailable on local machines.
                archive_error = str(exc)

        return RetrievalResult(
            goals=[GoalRead.model_validate(goal) for goal in store.list_goals(GoalStatus.ACTIVE)],
            projects=[
                ProjectRead.model_validate(project)
                for project in store.list_projects(ProjectStatus.ACTIVE)
            ],
            profile=[ProfileFactRead.model_validate(fact) for fact in store.list_profile()],
            preferences=[
                PreferenceRead.model_validate(preference)
                for preference in store.list_preferences()
            ],
            relevant_memories=[MemoryRead.model_validate(memory) for memory in memories],
            events=[
                EventRead.model_validate(event)
                for event in store.search_events(request.query)
            ],
            archive_results=archive_results,
            archive_error=archive_error,
        )

    def search_all(self, store: MemoryStore, query: str, limit: int = 10) -> dict[str, list[Any]]:
        return {
            "goals": [GoalRead.model_validate(item) for item in store.search_goals(query, limit)],
            "projects": [
                ProjectRead.model_validate(item) for item in store.search_projects(query, limit)
            ],
            "events": [
                EventRead.model_validate(item)
                for item in store.search_events(query, limit)
            ],
            "memories": [
                MemoryRead.model_validate(item) for item in store.search_memories(query, limit)
            ],
        }

    def _archives(self) -> QdrantArchiveService:
        if self.archive_service is None:
            if not self.enable_default_archives:
                raise RuntimeError("Qdrant archive service is not configured.")
            self.archive_service = QdrantArchiveService()
        return self.archive_service
