from __future__ import annotations

from pydantic import BaseModel

from app.repositories.memory_store import MemoryStore
from app.services.retrieval import RetrievalRequest, RetrievalResult, RetrievalService


class ContextPackage(BaseModel):
    profile: list
    preferences: list
    goals: list
    projects: list
    relevant_memories: list
    events: list
    archive_results: list


class ContextAssemblyService:
    """Build the structured context package sent to Neo before generation."""

    def __init__(self, retrieval: RetrievalService | None = None) -> None:
        self.retrieval = retrieval or RetrievalService()

    def assemble(self, store: MemoryStore, request: RetrievalRequest) -> ContextPackage:
        result = self.retrieval.retrieve(store, request)
        return self.from_retrieval(result)

    def from_retrieval(self, result: RetrievalResult) -> ContextPackage:
        return ContextPackage(
            profile=result.profile,
            preferences=result.preferences,
            goals=result.goals,
            projects=result.projects,
            relevant_memories=result.relevant_memories,
            events=result.events,
            archive_results=result.archive_results,
        )
