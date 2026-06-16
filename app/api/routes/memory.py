from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_store
from app.models.enums import CandidateStatus, GoalStatus, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.schemas.memory_objects import (
    EventRead,
    GoalRead,
    MemoryCandidateRead,
    MemoryRead,
    PreferenceRead,
    ProfileFactRead,
    ProjectRead,
)
from app.services.archives import QdrantArchiveService
from app.services.context import ContextAssemblyService, ContextPackage
from app.services.extraction import ExtractionRequest, ExtractionResult, MemoryExtractionService
from app.services.reflection import ReflectionRunRequest, ReflectionRunResult, ReflectionService
from app.services.retrieval import RetrievalRequest
from app.services.review import MemoryReviewRequest, MemoryReviewResult, MemoryReviewService

router = APIRouter()
StoreDependency = Annotated[MemoryStore, Depends(get_store)]


@router.post("/conversation", response_model=ExtractionResult)
def ingest_conversation(
    request: ExtractionRequest,
    store: StoreDependency,
) -> ExtractionResult:
    service = MemoryExtractionService()
    result = service.extract(request)
    if request.persist:
        service.persist_and_accept(store, result)
        store.db.commit()
    try:
        text = request.text or "\n".join(message.content for message in request.messages)
        if text.strip():
            QdrantArchiveService().archive_text(
                "conversation_archive",
                text,
                {"source": "conversation"},
            )
    except Exception:
        pass
    return result


@router.post("/extract-memory", response_model=ExtractionResult)
def extract_memory(
    request: ExtractionRequest,
    store: StoreDependency,
) -> ExtractionResult:
    service = MemoryExtractionService()
    result = service.extract(request)
    if request.persist:
        service.persist_and_accept(store, result)
        store.db.commit()
    return result


@router.post("/retrieve-context", response_model=ContextPackage)
def retrieve_context(
    request: RetrievalRequest,
    store: StoreDependency,
) -> ContextPackage:
    package = ContextAssemblyService().assemble(store, request)
    store.db.commit()
    return package


@router.post("/memory/review", response_model=MemoryReviewResult)
def review_memory(
    request: MemoryReviewRequest,
    store: StoreDependency,
) -> MemoryReviewResult:
    try:
        result = MemoryReviewService().review(store, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.db.commit()
    return result


@router.post("/reflection/run", response_model=ReflectionRunResult)
def run_reflection(
    store: StoreDependency,
    request: ReflectionRunRequest | None = None,
) -> ReflectionRunResult:
    result = ReflectionService().run(store, request or ReflectionRunRequest())
    store.db.commit()
    return result


@router.get("/goals", response_model=list[GoalRead])
def list_goals(store: StoreDependency) -> list[GoalRead]:
    return [GoalRead.model_validate(goal) for goal in store.list_goals(GoalStatus.ACTIVE)]


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(store: StoreDependency) -> list[ProjectRead]:
    return [
        ProjectRead.model_validate(project)
        for project in store.list_projects(ProjectStatus.ACTIVE)
    ]


@router.get("/events", response_model=list[EventRead])
def list_events(store: StoreDependency) -> list[EventRead]:
    return [EventRead.model_validate(event) for event in store.list_events()]


@router.get("/memories", response_model=list[MemoryRead])
def list_memories(store: StoreDependency) -> list[MemoryRead]:
    return [MemoryRead.model_validate(memory) for memory in store.list_memories()]


@router.get("/memory/candidates", response_model=list[MemoryCandidateRead])
def list_memory_candidates(
    store: StoreDependency,
    status: CandidateStatus | None = CandidateStatus.PENDING,
) -> list[MemoryCandidateRead]:
    return [
        MemoryCandidateRead.model_validate(candidate)
        for candidate in store.list_candidates(status=status)
    ]


@router.get("/profile", response_model=list[ProfileFactRead])
def list_profile(store: StoreDependency) -> list[ProfileFactRead]:
    return [ProfileFactRead.model_validate(fact) for fact in store.list_profile()]


@router.get("/preferences", response_model=list[PreferenceRead])
def list_preferences(store: StoreDependency) -> list[PreferenceRead]:
    return [
        PreferenceRead.model_validate(preference)
        for preference in store.list_preferences()
    ]
