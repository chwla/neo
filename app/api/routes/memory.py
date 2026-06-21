from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_store
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import ChatMessage
from app.models.enums import CandidateStatus, GoalStatus, MemoryType, ProjectStatus
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
from app.services.chat import NeoChatService
from app.services.context import ContextAssemblyService, ContextPackage
from app.services.extraction import ExtractionRequest, ExtractionResult, MemoryExtractionService
from app.services.explanation import MemoryExplanation, MemoryExplanationService
from app.services.lifecycle import AgingPolicy, MemoryLifecycleService
from app.services.lifecycle_maintenance import MemoryLifecycleMaintenance
from app.services.ollama_client import OllamaClient
from app.services.reflection import ReflectionRunRequest, ReflectionRunResult, ReflectionService
from app.services.retrieval import RetrievalRequest
from app.services.review import MemoryReviewRequest, MemoryReviewResult, MemoryReviewService

router = APIRouter()
StoreDependency = Annotated[MemoryStore, Depends(get_store)]


def _ollama_client() -> OllamaClient:
    settings = get_settings()
    return OllamaClient(
        model=settings.chat_model,
        base_url=settings.ollama_url,
        timeout=settings.chat_timeout_seconds,
        num_predict=settings.chat_num_predict,
    )


def extract_after_turn_background(user_prompt: str, assistant_reply: str) -> None:
    db = SessionLocal()
    try:
        service = NeoChatService(
            db,
            ollama=_ollama_client(),
        )
        service.extract_after_turn(user_prompt, assistant_reply)
    except Exception:
        db.rollback()
    finally:
        db.close()


class ChatRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    project_id: int | None
    archived: bool
    created_at: datetime
    updated_at: datetime


class ChatMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    chat_id: int
    role: str
    content: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    thinking: str | None = None
    created_at: datetime


class ProjectWithChatsRead(ProjectRead):
    chats: list[ChatRead] = Field(default_factory=list)


class SidebarRead(BaseModel):
    projects: list[ProjectWithChatsRead]
    chats: list[ChatRead]


class ChatThreadRead(BaseModel):
    chat: ChatRead
    messages: list[ChatMessageRead]


class ChatCreateRequest(BaseModel):
    project_id: int | None = None


class ChatSendRequest(BaseModel):
    prompt: str = Field(min_length=1)


class ChatMessageUpdateRequest(BaseModel):
    content: str = Field(min_length=1)


class ChatSendResponse(BaseModel):
    chat: ChatRead
    messages: list[ChatMessageRead]
    reply: str
    web_debug: dict[str, object] = Field(default_factory=dict)


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1)


class ProfileUpdateRequest(BaseModel):
    key: str = Field(min_length=1)
    value: str = Field(min_length=1)


class PreferenceUpdateRequest(BaseModel):
    category: str = Field(min_length=1)
    value: str = Field(min_length=1)
    importance: int = Field(ge=1, le=10)


class GoalUpdateRequest(BaseModel):
    goal: str = Field(min_length=1)
    description: str | None = None
    priority: int = Field(ge=1, le=10)


class ProjectUpdateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    priority: int = Field(ge=1, le=10)


class EventUpdateRequest(BaseModel):
    event: str = Field(min_length=1)
    description: str | None = None
    event_date: date | None = None
    importance: int = Field(ge=1, le=10)


class MemoryUpdateRequest(BaseModel):
    memory_text: str = Field(min_length=1)
    memory_type: MemoryType
    importance: int = Field(ge=1, le=10)


class MemoryExplainRequest(BaseModel):
    query: str = Field(min_length=1)


class MemoryLifecycleActionRequest(BaseModel):
    reason: str = Field(default="Manual lifecycle operation.", min_length=1)


class MemorySupersedeRequest(BaseModel):
    replacement_memory_id: int
    reason: str = Field(default="Manual memory supersession.", min_length=1)


class MemoryRestoreRequest(BaseModel):
    restore_intent: str = Field(pattern="^restore$")
    reason: str = Field(default="Explicit manual memory restore.", min_length=1)


class MemoryLifecycleActionResponse(BaseModel):
    memory_id: int
    status: str
    related_memory_id: int | None = None


class MemoryLifecycleAuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    memory_id: int
    action: str
    created_at: datetime
    previous_status: str | None
    new_status: str | None
    reason: str | None
    related_memory_id: int | None
    source_sentence: str | None


class MemoryAgingRequest(BaseModel):
    dry_run: bool = True
    dormant_days: int = Field(default=180, ge=1)
    archive_importance_below: int = Field(default=4, ge=1, le=10)
    confidence_decay: float = Field(default=0.05, ge=0, le=1)


class MemoryAgingResponse(BaseModel):
    dry_run: bool
    archived: int
    decayed: int
    skipped: int
    actions: list[dict]


class MemoryLifecycleMaintenanceRequest(BaseModel):
    apply: bool = False
    confirm: str | None = None
    max_actions: int = Field(default=10, ge=0)
    include_aging: bool = True
    include_compression_candidates: bool = True
    include_audit_check: bool = True
    include_tombstone_review: bool = True
    include_audit_repair: bool = False


class MemoryLifecycleMaintenanceResponse(BaseModel):
    dry_run: bool
    max_actions: int
    planned_actions: list[dict]
    applied_actions: list[dict]
    skipped_actions: list[dict]
    warnings: list[str]
    aging: dict
    compression_candidates: list[dict]
    audit_consistency: list[dict]
    audit_repair: dict
    tombstone_review: dict


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _get_required_chat(store: MemoryStore, chat_id: int):
    chat = store.get_chat(chat_id)
    if chat is None or chat.archived:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


def _get_required_project(store: MemoryStore, project_id: int):
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_required_memory(store: MemoryStore, memory_id: int):
    memory = store.get_memory(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return memory


def _thread_payload(store: MemoryStore, chat_id: int) -> ChatThreadRead:
    chat = _get_required_chat(store, chat_id)
    messages = store.list_chat_messages(chat_id)
    return ChatThreadRead(
        chat=ChatRead.model_validate(chat),
        messages=[ChatMessageRead.model_validate(message) for message in messages],
    )


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


@router.get("/sidebar", response_model=SidebarRead)
def get_sidebar(store: StoreDependency) -> SidebarRead:
    projects = []
    for project in store.list_projects(ProjectStatus.ACTIVE):
        chats = store.list_chats(project_id=project.id, with_messages_only=True, limit=12)
        project_data = ProjectRead.model_validate(project).model_dump()
        projects.append(
            ProjectWithChatsRead(
                **project_data,
                chats=[ChatRead.model_validate(chat) for chat in chats],
            )
        )
    chats = store.list_chats(unprojected_only=True, with_messages_only=True, limit=20)
    return SidebarRead(
        projects=projects,
        chats=[ChatRead.model_validate(chat) for chat in chats],
    )


@router.post("/chats", response_model=ChatRead, status_code=status.HTTP_201_CREATED)
def create_chat(request: ChatCreateRequest, store: StoreDependency) -> ChatRead:
    if request.project_id is not None:
        _get_required_project(store, request.project_id)
    chat = store.create_chat(project_id=request.project_id)
    store.db.commit()
    store.db.refresh(chat)
    return ChatRead.model_validate(chat)


@router.get("/chats/{chat_id}", response_model=ChatThreadRead)
def get_chat(chat_id: int, store: StoreDependency) -> ChatThreadRead:
    return _thread_payload(store, chat_id)


@router.post("/chats/{chat_id}/messages", response_model=ChatSendResponse)
def send_chat_message(
    chat_id: int,
    request: ChatSendRequest,
    store: StoreDependency,
) -> ChatSendResponse:
    _get_required_chat(store, chat_id)
    service = NeoChatService(
        store.db,
        ollama=_ollama_client(),
    )
    try:
        reply = service.send_message(chat_id, request.prompt)
    except Exception as exc:
        settings = get_settings()
        raise HTTPException(
            status_code=502,
            detail=(
                f"Ollama did not finish the response. Expected {settings.chat_model} "
                f"at {settings.ollama_url} within {settings.chat_timeout_seconds} seconds. "
                f"Details: {exc}"
            ),
        ) from exc
    payload = _thread_payload(store, chat_id)
    return ChatSendResponse(
        chat=payload.chat,
        messages=payload.messages,
        reply=reply,
        web_debug=service.last_web_debug,
    )


@router.post("/chats/{chat_id}/messages/stream")
def stream_chat_message(
    chat_id: int,
    request: ChatSendRequest,
    background_tasks: BackgroundTasks,
    store: StoreDependency,
) -> StreamingResponse:
    _get_required_chat(store, chat_id)
    service = NeoChatService(
        store.db,
        ollama=_ollama_client(),
    )

    def events():
        try:
            for event in service.stream_message(
                chat_id,
                request.prompt,
                after_reply=lambda prompt, reply: background_tasks.add_task(
                    extract_after_turn_background,
                    prompt,
                    reply,
                ),
            ):
                yield json.dumps(event, default=str) + "\n"
        except Exception as exc:
            settings = get_settings()
            yield json.dumps(
                {
                    "type": "error",
                    "detail": (
                        f"Ollama did not finish the response. Expected {settings.chat_model} "
                        f"at {settings.ollama_url} within {settings.chat_timeout_seconds} seconds. "
                        f"Details: {exc}"
                    ),
                    "web_debug": service.last_web_debug,
                }
            ) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.patch("/chats/{chat_id}/messages/{message_id}", response_model=ChatMessageRead)
def update_chat_message(
    chat_id: int,
    message_id: int,
    request: ChatMessageUpdateRequest,
    store: StoreDependency,
) -> ChatMessageRead:
    _get_required_chat(store, chat_id)
    message = store.db.get(ChatMessage, message_id)
    if message is None or message.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.role != "user":
        raise HTTPException(status_code=400, detail="Only user messages can be edited")
    cleaned = request.content.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="Message content is required")
    message = store.update_chat_message_content(message_id, cleaned)
    store.db.commit()
    store.db.refresh(message)
    return ChatMessageRead.model_validate(message)


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: int, store: StoreDependency) -> Response:
    _get_required_chat(store, chat_id)
    store.delete_chat(chat_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/goals", response_model=list[GoalRead])
def list_goals(store: StoreDependency) -> list[GoalRead]:
    return [GoalRead.model_validate(goal) for goal in store.list_goals(GoalStatus.ACTIVE)]


@router.patch("/goals/{goal_id}", response_model=GoalRead)
def update_goal(
    goal_id: int,
    request: GoalUpdateRequest,
    store: StoreDependency,
) -> GoalRead:
    if store.get_goal(goal_id) is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    store.update_goal(
        goal_id,
        request.goal.strip(),
        _clean_optional_text(request.description),
        request.priority,
    )
    store.db.commit()
    goal = store.get_goal(goal_id)
    return GoalRead.model_validate(goal)


@router.delete("/goals/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(goal_id: int, store: StoreDependency) -> Response:
    if store.get_goal(goal_id) is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    store.delete_goal(goal_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(store: StoreDependency) -> list[ProjectRead]:
    return [
        ProjectRead.model_validate(project)
        for project in store.list_projects(ProjectStatus.ACTIVE)
    ]


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(request: ProjectCreateRequest, store: StoreDependency) -> ProjectRead:
    cleaned = " ".join(request.name.split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="Project name is required")
    project = store.create_project(cleaned)
    store.db.commit()
    store.db.refresh(project)
    return ProjectRead.model_validate(project)


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: int,
    request: ProjectUpdateRequest,
    store: StoreDependency,
) -> ProjectRead:
    _get_required_project(store, project_id)
    store.update_project_memory(
        project_id,
        request.name.strip(),
        _clean_optional_text(request.description),
        request.priority,
    )
    store.db.commit()
    project = store.get_project(project_id)
    return ProjectRead.model_validate(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, store: StoreDependency) -> Response:
    _get_required_project(store, project_id)
    store.delete_project(project_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/projects/{project_id}/memory", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_memory(project_id: int, store: StoreDependency) -> Response:
    _get_required_project(store, project_id)
    store.delete_project_memory(project_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/events", response_model=list[EventRead])
def list_events(store: StoreDependency) -> list[EventRead]:
    return [EventRead.model_validate(event) for event in store.list_events()]


@router.patch("/events/{event_id}", response_model=EventRead)
def update_event(
    event_id: int,
    request: EventUpdateRequest,
    store: StoreDependency,
) -> EventRead:
    if store.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="Event not found")
    store.update_event(
        event_id,
        request.event.strip(),
        _clean_optional_text(request.description),
        request.event_date,
        request.importance,
    )
    store.db.commit()
    event = store.get_event(event_id)
    return EventRead.model_validate(event)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: int, store: StoreDependency) -> Response:
    if store.get_event(event_id) is None:
        raise HTTPException(status_code=404, detail="Event not found")
    store.delete_event(event_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/memories", response_model=list[MemoryRead])
def list_memories(store: StoreDependency) -> list[MemoryRead]:
    return [MemoryRead.model_validate(memory) for memory in store.list_memories()]


@router.patch("/memories/{memory_id}", response_model=MemoryRead)
def update_memory(
    memory_id: int,
    request: MemoryUpdateRequest,
    store: StoreDependency,
) -> MemoryRead:
    if store.get_memory(memory_id) is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    store.update_memory(
        memory_id,
        request.memory_text.strip(),
        request.memory_type,
        request.importance,
    )
    store.db.commit()
    memory = store.get_memory(memory_id)
    return MemoryRead.model_validate(memory)


@router.delete("/memories/{memory_id}", response_model=MemoryLifecycleActionResponse)
def delete_memory(memory_id: int, store: StoreDependency) -> MemoryLifecycleActionResponse:
    memory = _get_required_memory(store, memory_id)
    store.delete_memory(memory_id)
    store.db.commit()
    store.db.refresh(memory)
    return MemoryLifecycleActionResponse(memory_id=memory.id, status=memory.status)


@router.post("/memories/{memory_id}/archive", response_model=MemoryLifecycleActionResponse)
def archive_memory(
    memory_id: int,
    request: MemoryLifecycleActionRequest,
    store: StoreDependency,
) -> MemoryLifecycleActionResponse:
    memory = _get_required_memory(store, memory_id)
    MemoryLifecycleService().archive(store, memory, request.reason)
    store.db.commit()
    store.db.refresh(memory)
    return MemoryLifecycleActionResponse(memory_id=memory.id, status=memory.status)


@router.post("/memories/{memory_id}/supersede", response_model=MemoryLifecycleActionResponse)
def supersede_memory(
    memory_id: int,
    request: MemorySupersedeRequest,
    store: StoreDependency,
) -> MemoryLifecycleActionResponse:
    old_memory = _get_required_memory(store, memory_id)
    new_memory = _get_required_memory(store, request.replacement_memory_id)
    if not new_memory.is_active or new_memory.status != "active":
        raise HTTPException(status_code=400, detail="Replacement memory must be active")
    MemoryLifecycleService().supersede(store, old_memory, new_memory, request.reason)
    store.db.commit()
    store.db.refresh(old_memory)
    return MemoryLifecycleActionResponse(
        memory_id=old_memory.id,
        status=old_memory.status,
        related_memory_id=new_memory.id,
    )


@router.post("/memories/{memory_id}/restore", response_model=MemoryLifecycleActionResponse)
def restore_memory(
    memory_id: int,
    request: MemoryRestoreRequest,
    store: StoreDependency,
) -> MemoryLifecycleActionResponse:
    memory = _get_required_memory(store, memory_id)
    try:
        MemoryLifecycleService().restore(
            store,
            memory,
            request.reason,
            explicit_restore=request.restore_intent == "restore",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.db.commit()
    store.db.refresh(memory)
    return MemoryLifecycleActionResponse(memory_id=memory.id, status=memory.status)


@router.get("/memories/{memory_id}/lifecycle", response_model=list[MemoryLifecycleAuditRead])
def memory_lifecycle_history(
    memory_id: int,
    store: StoreDependency,
) -> list[MemoryLifecycleAuditRead]:
    _get_required_memory(store, memory_id)
    return [
        MemoryLifecycleAuditRead.model_validate(record)
        for record in store.list_lifecycle_audit(memory_id)
    ]


@router.post("/memory/lifecycle/age", response_model=MemoryAgingResponse)
def age_memory_lifecycle(
    request: MemoryAgingRequest,
    store: StoreDependency,
) -> MemoryAgingResponse:
    result = store.age_memories(
        AgingPolicy(
            dormant_days=request.dormant_days,
            archive_importance_below=request.archive_importance_below,
            confidence_decay=request.confidence_decay,
        ),
        dry_run=request.dry_run,
    )
    if not request.dry_run:
        store.db.commit()
    return MemoryAgingResponse(
        dry_run=result.dry_run,
        archived=result.archived,
        decayed=result.decayed,
        skipped=result.skipped,
        actions=list(result.actions),
    )


@router.post("/memory/lifecycle/maintenance", response_model=MemoryLifecycleMaintenanceResponse)
def run_memory_lifecycle_maintenance(
    request: MemoryLifecycleMaintenanceRequest,
    store: StoreDependency,
) -> MemoryLifecycleMaintenanceResponse:
    if request.apply and request.confirm != "APPLY_LIFECYCLE_MAINTENANCE":
        raise HTTPException(
            status_code=400,
            detail="Apply mode requires confirm='APPLY_LIFECYCLE_MAINTENANCE'.",
        )
    report = MemoryLifecycleMaintenance().run(
        store,
        apply=request.apply,
        max_actions=request.max_actions,
        include_aging=request.include_aging,
        include_compression_candidates=request.include_compression_candidates,
        include_audit_check=request.include_audit_check,
        include_tombstone_review=request.include_tombstone_review,
        include_audit_repair=request.include_audit_repair,
    )
    if request.apply:
        store.db.commit()
    else:
        store.db.rollback()
    return MemoryLifecycleMaintenanceResponse(**report.to_dict())


@router.get("/memory/candidates", response_model=list[MemoryCandidateRead])
def list_memory_candidates(
    store: StoreDependency,
    status: CandidateStatus | None = CandidateStatus.PENDING,
) -> list[MemoryCandidateRead]:
    return [
        MemoryCandidateRead.model_validate(candidate)
        for candidate in store.list_candidates(status=status)
    ]


@router.post("/memory/explain", response_model=MemoryExplanation)
def explain_memory(
    request: MemoryExplainRequest,
    store: StoreDependency,
) -> MemoryExplanation:
    return MemoryExplanationService().explain(store, request.query)


@router.get("/profile", response_model=list[ProfileFactRead])
def list_profile(store: StoreDependency) -> list[ProfileFactRead]:
    return [ProfileFactRead.model_validate(fact) for fact in store.list_profile()]


@router.patch("/profile/{profile_id}", response_model=ProfileFactRead)
def update_profile(
    profile_id: int,
    request: ProfileUpdateRequest,
    store: StoreDependency,
) -> ProfileFactRead:
    if store.get_profile_fact(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile fact not found")
    store.update_profile_fact(profile_id, request.key.strip(), request.value.strip())
    store.db.commit()
    fact = store.get_profile_fact(profile_id)
    return ProfileFactRead.model_validate(fact)


@router.delete("/profile/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_profile(profile_id: int, store: StoreDependency) -> Response:
    if store.get_profile_fact(profile_id) is None:
        raise HTTPException(status_code=404, detail="Profile fact not found")
    store.delete_profile_fact(profile_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/preferences", response_model=list[PreferenceRead])
def list_preferences(store: StoreDependency) -> list[PreferenceRead]:
    return [
        PreferenceRead.model_validate(preference)
        for preference in store.list_preferences()
    ]


@router.patch("/preferences/{preference_id}", response_model=PreferenceRead)
def update_preference(
    preference_id: int,
    request: PreferenceUpdateRequest,
    store: StoreDependency,
) -> PreferenceRead:
    if store.get_preference(preference_id) is None:
        raise HTTPException(status_code=404, detail="Preference not found")
    store.update_preference(
        preference_id,
        request.category.strip(),
        request.value.strip(),
        request.importance,
    )
    store.db.commit()
    preference = store.get_preference(preference_id)
    return PreferenceRead.model_validate(preference)


@router.delete("/preferences/{preference_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preference(preference_id: int, store: StoreDependency) -> Response:
    if store.get_preference(preference_id) is None:
        raise HTTPException(status_code=404, detail="Preference not found")
    store.delete_preference(preference_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
