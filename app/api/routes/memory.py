from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from threading import Lock, Thread
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_store
from app.api.routes.accounts import session_for
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Chat, ChatGeneration, ChatMessage
from app.models.enums import CandidateStatus, GoalStatus, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.schemas.memory_objects import (
    ActivityRead,
    EducationRead,
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
from app.services.explanation import MemoryExplanation, MemoryExplanationService
from app.services.extraction import ExtractionRequest, ExtractionResult, MemoryExtractionService
from app.services.lifecycle import AgingPolicy, MemoryLifecycleService
from app.services.lifecycle_maintenance import MemoryLifecycleMaintenance
from app.services.llm import LLMClient, LLMRegistry, get_llm_client
from app.services.profile_accounts import profile_database
from app.services.reflection import ReflectionRunRequest, ReflectionRunResult, ReflectionService
from app.services.retrieval import RetrievalRequest
from app.services.review import MemoryReviewRequest, MemoryReviewResult, MemoryReviewService
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest

router = APIRouter()
StoreDependency = Annotated[MemoryStore, Depends(get_store)]
PROCESS_WORKER_ID = str(uuid.uuid4())
GENERATION_LEASE_SECONDS = 120
_GENERATION_THREADS: set[str] = set()
_GENERATION_THREADS_LOCK = Lock()


def _llm_client(config_id: str | None = None, route_name: str = "chat") -> LLMClient:
    try:
        return get_llm_client(config_id, route_name=route_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
    response_kind: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    route_name: str | None = None
    finish_reason: str | None = None
    trace_id: str | None = None
    generation_id: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    search_trace: dict[str, object] = Field(default_factory=dict)
    connector_trace: dict[str, object] = Field(default_factory=dict)
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
    llm_id: str | None = Field(default=None, max_length=80)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=80)
    timezone: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=40)

    @field_validator("timezone", mode="before")
    @classmethod
    def normalize_timezone(cls, value: object) -> str | None:
        """Accept valid browser timezones without letting optional metadata reject chat.

        The timezone is client-supplied context, not part of the user prompt.  A browser can
        report an IANA backwards-compatibility name (for example ``Asia/Calcutta``), and a
        stale or malformed value must simply fall back to the profile/default timezone.
        """
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 80:
            return None
        try:
            return ZoneInfo(cleaned).key
        except (ZoneInfoNotFoundError, ValueError):
            return None

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class ChatMessageUpdateRequest(BaseModel):
    content: str = Field(min_length=1)


class ChatSendResponse(BaseModel):
    chat: ChatRead
    messages: list[ChatMessageRead]
    reply: str
    web_debug: dict[str, object] = Field(default_factory=dict)


class ChatGenerationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: int
    status: str
    status_detail: str | None = None
    partial_response: str
    thinking: str | None = None
    reply: str | None = None
    error: str | None = None
    timezone: str | None = None
    locale: str | None = None
    response_kind: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    route_name: str | None = None
    finish_reason: str | None = None
    trace_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    duration_ms: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    search_trace: dict[str, object] = Field(default_factory=dict)
    connector_trace: dict[str, object] = Field(default_factory=dict)
    user_message_id: int | None = None
    assistant_message_id: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    heartbeat_at: datetime | None = None
    completed_at: datetime | None = None


class ChatGenerationStartResponse(BaseModel):
    generation: ChatGenerationRead


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
        messages=[_chat_message_read(message) for message in messages],
    )


def _json_object(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _chat_message_read(message: ChatMessage) -> ChatMessageRead:
    metadata = _json_object(message.metadata_json)
    search_trace = metadata.get("search_trace") or metadata.get("web_debug") or {}
    connector_trace = metadata.get("connector_trace") or {}
    payload = {
        field: getattr(message, field)
        for field in ChatMessageRead.model_fields
        if field not in {"metadata", "search_trace", "connector_trace"}
    }
    return ChatMessageRead.model_validate(
        {
            **payload,
            "metadata": metadata,
            "search_trace": search_trace if isinstance(search_trace, dict) else {},
            "connector_trace": connector_trace if isinstance(connector_trace, dict) else {},
        },
    )


def _generation_read(generation: ChatGeneration) -> ChatGenerationRead:
    metadata = _json_object(generation.metadata_json)
    search_trace = metadata.get("search_trace") or metadata.get("web_debug") or {}
    connector_trace = metadata.get("connector_trace") or {}
    payload = {
        field: getattr(generation, field)
        for field in ChatGenerationRead.model_fields
        if field not in {"metadata", "search_trace", "connector_trace"}
    }
    return ChatGenerationRead.model_validate(
        {
            **payload,
            "metadata": metadata,
            "search_trace": search_trace if isinstance(search_trace, dict) else {},
            "connector_trace": connector_trace if isinstance(connector_trace, dict) else {},
        },
    )


def _generation_service(db, chat: Chat, llm_id: str | None) -> NeoChatService:
    rule_result = RuleResolver().resolve(
        RuleResolveRequest(
            context_type="chat",
            context_id=str(chat.id),
            project_id=str(chat.project_id) if chat.project_id is not None else None,
        )
    )
    route_name = RuleResolver.route_name(rule_result, "chat", "chat")
    return NeoChatService(db, ollama=_llm_client(llm_id, route_name), rule_result=rule_result)


def _lease_cutoff(now: datetime | None = None) -> datetime:
    current = now or datetime.now(UTC)
    return current - timedelta(seconds=_lease_duration_seconds())


def _lease_duration_seconds() -> int:
    """Keep a valid lease longer than one configured provider request."""

    return max(GENERATION_LEASE_SECONDS, get_settings().chat_timeout_seconds + 60)


def _heartbeat_is_stale(heartbeat: datetime | None, *, now: datetime | None = None) -> bool:
    if heartbeat is None:
        return True
    current = now or datetime.now(UTC)
    if heartbeat.tzinfo is None:
        heartbeat = heartbeat.replace(tzinfo=UTC)
    return heartbeat <= _lease_cutoff(current)


def _claim_generation(
    db,
    generation_id: str,
    lease_token: str,
    *,
    now: datetime | None = None,
) -> ChatGeneration | None:
    """Atomically claim queued work or take over an expired running lease."""

    current = now or datetime.now(UTC)
    result = db.execute(
        update(ChatGeneration)
        .where(
            ChatGeneration.id == generation_id,
            or_(
                ChatGeneration.status == "queued",
                and_(
                    ChatGeneration.status == "running",
                    or_(
                        ChatGeneration.heartbeat_at.is_(None),
                        ChatGeneration.heartbeat_at <= _lease_cutoff(current),
                    ),
                ),
            ),
        )
        .values(
            status="running",
            status_detail="Preparing your response",
            worker_id=PROCESS_WORKER_ID,
            lease_token=lease_token,
            started_at=func.coalesce(ChatGeneration.started_at, current),
            heartbeat_at=current,
            error=None,
            completed_at=None,
            attempt_count=func.coalesce(ChatGeneration.attempt_count, 0) + 1,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    if result.rowcount != 1:
        return None
    return db.get(ChatGeneration, generation_id)


def _update_leased_generation(
    db,
    generation_id: str,
    lease_token: str,
    **values,
) -> bool:
    """Write worker state only while the caller still owns the lease."""

    result = db.execute(
        update(ChatGeneration)
        .where(
            ChatGeneration.id == generation_id,
            ChatGeneration.status == "running",
            ChatGeneration.worker_id == PROCESS_WORKER_ID,
            ChatGeneration.lease_token == lease_token,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    db.commit()
    return True


def _run_chat_generation(profile: dict, generation_id: str) -> None:
    """Finish a response independently of the browser connection."""

    with profile_database(profile["id"], guest=bool(profile.get("is_guest"))):
        db = SessionLocal()
        lease_token = str(uuid.uuid4())
        try:
            generation = _claim_generation(db, generation_id, lease_token)
            if generation is None:
                return
            chat = db.get(Chat, generation.chat_id)
            if chat is None or chat.archived:
                _update_leased_generation(
                    db,
                    generation_id,
                    lease_token,
                    status="failed",
                    status_detail="Failed",
                    error="Chat is no longer available.",
                    completed_at=datetime.now(UTC),
                )
                return

            service = _generation_service(db, chat, generation.llm_id)
            partial_response = ""
            thinking = ""
            for event in service.stream_message(
                chat.id,
                generation.prompt,
                existing_user_message_id=generation.user_message_id,
                timezone=generation.timezone,
                locale=generation.locale,
                generation_id=generation.id,
                generation_lease_token=lease_token,
            ):
                values: dict[str, object] = {"heartbeat_at": datetime.now(UTC)}
                if event["type"] == "chunk":
                    partial_response += str(event.get("content") or "")
                    values["partial_response"] = partial_response
                elif event["type"] == "thinking":
                    thinking += str(event.get("content") or "")
                    values["thinking"] = thinking
                elif event["type"] == "replace":
                    partial_response = str(event.get("content") or "")
                    values["partial_response"] = partial_response
                elif event["type"] == "status":
                    values["status_detail"] = str(event.get("content") or "")[:120] or None
                elif event["type"] == "done":
                    reply = str(event.get("reply") or partial_response)
                    values.update(
                        {
                            "status": "completed",
                            "status_detail": "Completed",
                            "reply": reply,
                            "partial_response": reply,
                            "thinking": str(event.get("thinking") or thinking) or None,
                            "assistant_message_id": event.get("message_id"),
                            "response_kind": event.get("response_kind"),
                            "provider_name": (event.get("provider_name") or event.get("provider")),
                            "model_name": event.get("model_name") or event.get("model"),
                            "route_name": event.get("route_name"),
                            "finish_reason": event.get("finish_reason"),
                            "trace_id": (event.get("trace_id") or event.get("provider_request_id")),
                            "prompt_tokens": event.get("prompt_tokens"),
                            "completion_tokens": event.get("completion_tokens"),
                            "total_tokens": event.get("total_tokens"),
                            "duration_ms": event.get("duration_ms"),
                            "metadata_json": json.dumps(
                                {
                                    "response_metadata": event.get("metadata") or {},
                                    "web_debug": event.get("web_debug") or {},
                                    "search_trace": event.get("search_trace") or {},
                                    "connector_trace": event.get("connector_trace") or {},
                                },
                                default=str,
                                sort_keys=True,
                            ),
                            "completed_at": datetime.now(UTC),
                        }
                    )
                if not _update_leased_generation(
                    db,
                    generation_id,
                    lease_token,
                    **values,
                ):
                    return

            generation = db.get(ChatGeneration, generation_id)
            if generation is not None and generation.status == "running":
                _update_leased_generation(
                    db,
                    generation_id,
                    lease_token,
                    status="failed",
                    status_detail="Failed",
                    error="The response ended without a completion event.",
                    completed_at=datetime.now(UTC),
                )
        except Exception as exc:
            db.rollback()
            _update_leased_generation(
                db,
                generation_id,
                lease_token,
                status="failed",
                status_detail="Failed",
                error=str(exc),
                completed_at=datetime.now(UTC),
            )
        finally:
            db.close()
            with _GENERATION_THREADS_LOCK:
                _GENERATION_THREADS.discard(generation_id)


def _spawn_generation(profile: dict, generation_id: str) -> None:
    """Start at most one generation worker in this process."""

    with _GENERATION_THREADS_LOCK:
        if generation_id in _GENERATION_THREADS:
            return
        _GENERATION_THREADS.add(generation_id)
    try:
        Thread(
            target=_run_chat_generation,
            args=(profile, generation_id),
            daemon=True,
            name=f"neo-chat-{generation_id[:8]}",
        ).start()
    except Exception:
        with _GENERATION_THREADS_LOCK:
            _GENERATION_THREADS.discard(generation_id)
        raise


def _recover_generation(
    request: Request,
    store: MemoryStore,
    generation: ChatGeneration,
) -> None:
    """Schedule queued work or work whose lease has verifiably expired."""

    if generation.status not in {"queued", "running"}:
        return
    profile = session_for(request)
    if profile is None:
        return
    if generation.status == "queued":
        _spawn_generation(profile, generation.id)
        return
    if _heartbeat_is_stale(generation.heartbeat_at):
        _spawn_generation(profile, generation.id)


def _start_chat_generation(
    request: Request,
    store: MemoryStore,
    chat_id: int,
    payload: ChatSendRequest,
    *,
    user_message_id: int | None = None,
) -> ChatGeneration:
    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="Choose a profile to continue.")
    chat = _get_required_chat(store, chat_id)
    ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
    if payload.client_request_id:
        existing = store.db.scalar(
            select(ChatGeneration).where(
                ChatGeneration.chat_id == chat.id,
                ChatGeneration.client_request_id == payload.client_request_id,
            )
        )
        if existing is not None:
            _recover_generation(request, store, existing)
            return existing
    generation_id = str(uuid.uuid4())
    cleaned_prompt = payload.prompt.strip()
    if user_message_id is None:
        user_message = store.add_chat_message(
            chat.id,
            "user",
            cleaned_prompt,
            metadata={
                "generation_id": generation_id,
                "client_request_id": payload.client_request_id,
            },
        )
        store.rename_chat_from_prompt(chat.id, cleaned_prompt)
        user_message_id = user_message.id
    else:
        user_message = store.db.get(ChatMessage, user_message_id)
        if user_message is None or user_message.chat_id != chat.id or user_message.role != "user":
            raise HTTPException(status_code=404, detail="User message not found")
    generation = ChatGeneration(
        id=generation_id,
        chat_id=chat.id,
        prompt=cleaned_prompt,
        llm_id=payload.llm_id,
        client_request_id=payload.client_request_id,
        user_message_id=user_message_id,
        status="queued",
        status_detail="Queued",
        timezone=payload.timezone,
        locale=payload.locale,
        worker_id=None,
        lease_token=None,
        heartbeat_at=None,
        attempt_count=0,
    )
    store.db.add(generation)
    try:
        store.db.commit()
    except IntegrityError:
        store.db.rollback()
        if payload.client_request_id:
            existing = store.db.scalar(
                select(ChatGeneration).where(
                    ChatGeneration.chat_id == chat.id,
                    ChatGeneration.client_request_id == payload.client_request_id,
                )
            )
            if existing is not None:
                _recover_generation(request, store, existing)
                return existing
        raise
    store.db.refresh(generation)
    _spawn_generation(profile, generation.id)
    return generation


def _supersede_generations_for_messages(
    db,
    chat_id: int,
    message_ids: list[int],
) -> None:
    """Fence active workers before an edit removes or changes their source turn."""

    if not message_ids:
        return
    db.execute(
        update(ChatGeneration)
        .where(
            ChatGeneration.chat_id == chat_id,
            ChatGeneration.user_message_id.in_(message_ids),
            ChatGeneration.status.in_(("queued", "running")),
        )
        .values(
            status="failed",
            status_detail="Superseded",
            error="The source user message was edited before this response completed.",
            completed_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
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
    chat = _get_required_chat(store, chat_id)
    rule_result = RuleResolver().resolve(
        RuleResolveRequest(
            context_type="chat",
            context_id=str(chat_id),
            project_id=str(chat.project_id) if chat.project_id is not None else None,
        )
    )
    route_name = RuleResolver.route_name(rule_result, "chat", "chat")
    service = NeoChatService(
        store.db,
        ollama=_llm_client(request.llm_id, route_name),
        rule_result=rule_result,
    )
    try:
        reply = service.send_message(
            chat_id,
            request.prompt,
            timezone=request.timezone,
            locale=request.locale,
        )
    except Exception as exc:
        config = LLMRegistry().get(request.llm_id)
        raise HTTPException(
            status_code=502,
            detail=(
                f"{config.name} did not finish the response. Expected {config.model} "
                f"at {config.base_url} within {config.timeout_seconds} seconds. "
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


@router.post("/chats/{chat_id}/generations", response_model=ChatGenerationStartResponse)
def start_chat_generation(
    chat_id: int,
    payload: ChatSendRequest,
    request: Request,
    store: StoreDependency,
) -> ChatGenerationStartResponse:
    if not payload.prompt.strip():
        raise HTTPException(status_code=422, detail="Message content is required")
    generation = _start_chat_generation(request, store, chat_id, payload)
    return ChatGenerationStartResponse(generation=_generation_read(generation))


@router.get("/chats/{chat_id}/generations/active", response_model=ChatGenerationRead | None)
def active_chat_generation(
    chat_id: int,
    request: Request,
    store: StoreDependency,
) -> ChatGenerationRead | None:
    _get_required_chat(store, chat_id)
    ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
    generation = store.db.scalar(
        select(ChatGeneration)
        .where(ChatGeneration.chat_id == chat_id, ChatGeneration.status.in_(("queued", "running")))
        .order_by(ChatGeneration.created_at.desc())
    )
    if generation is not None:
        _recover_generation(request, store, generation)
        store.db.refresh(generation)
    return _generation_read(generation) if generation is not None else None


@router.get("/chats/{chat_id}/generations/{generation_id}", response_model=ChatGenerationRead)
def get_chat_generation(
    chat_id: int,
    generation_id: str,
    request: Request,
    store: StoreDependency,
) -> ChatGenerationRead:
    _get_required_chat(store, chat_id)
    ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
    generation = store.db.get(ChatGeneration, generation_id)
    if generation is None or generation.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Chat generation not found")
    _recover_generation(request, store, generation)
    store.db.refresh(generation)
    return _generation_read(generation)


@router.post("/chats/{chat_id}/messages/stream")
def stream_chat_message(
    chat_id: int,
    request: ChatSendRequest,
    store: StoreDependency,
) -> StreamingResponse:
    chat = _get_required_chat(store, chat_id)
    rule_result = RuleResolver().resolve(
        RuleResolveRequest(
            context_type="chat",
            context_id=str(chat_id),
            project_id=str(chat.project_id) if chat.project_id is not None else None,
        )
    )
    route_name = RuleResolver.route_name(rule_result, "chat", "chat")
    service = NeoChatService(
        store.db,
        ollama=_llm_client(request.llm_id, route_name),
        rule_result=rule_result,
    )

    def events():
        try:
            for event in service.stream_message(
                chat_id,
                request.prompt,
                timezone=request.timezone,
                locale=request.locale,
            ):
                yield json.dumps(event, default=str) + "\n"
        except Exception as exc:
            config = LLMRegistry().get(request.llm_id)
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "detail": (
                            f"{config.name} did not finish the response. Expected {config.model} "
                            f"at {config.base_url} within {config.timeout_seconds} seconds. "
                            f"Details: {exc}"
                        ),
                        "web_debug": service.last_web_debug,
                    }
                )
                + "\n"
            )

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
    try:
        _supersede_generations_for_messages(store.db, chat_id, [message_id])
        store.detach_memory_sources_for_message(message_id, reason="replacement")
        message = store.update_chat_message_content(message_id, cleaned)
        extraction_request = ExtractionRequest(
            text=cleaned,
            persist=True,
            source_conversation_id=chat_id,
            source_message_id=message_id,
            source_timestamp=message.created_at if message is not None else None,
        )
        extractor = MemoryExtractionService()
        extraction = extractor.extract(extraction_request)
        extractor.persist_and_accept(store, extraction)
        store.db.commit()
    except Exception as exc:
        store.db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"The message could not be updated safely: {exc}",
        ) from exc
    store.db.refresh(message)
    return _chat_message_read(message)


@router.post(
    "/chats/{chat_id}/messages/{message_id}/rerun",
    response_model=ChatGenerationStartResponse,
)
def rerun_edited_chat_message(
    chat_id: int,
    message_id: int,
    payload: ChatSendRequest,
    request: Request,
    store: StoreDependency,
) -> ChatGenerationStartResponse:
    _get_required_chat(store, chat_id)
    ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
    if payload.client_request_id:
        existing_generation = store.db.scalar(
            select(ChatGeneration).where(
                ChatGeneration.chat_id == chat_id,
                ChatGeneration.client_request_id == payload.client_request_id,
            )
        )
        if existing_generation is not None:
            _recover_generation(request, store, existing_generation)
            return ChatGenerationStartResponse(generation=_generation_read(existing_generation))
    message = store.db.get(ChatMessage, message_id)
    if message is None or message.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Message not found")
    if message.role != "user":
        raise HTTPException(status_code=400, detail="Only user messages can be edited")
    cleaned = payload.prompt.strip()
    if not cleaned:
        raise HTTPException(status_code=422, detail="Message content is required")
    messages_after = list(
        store.db.scalars(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.id > message_id,
            )
        )
    )
    affected_message_ids = [message_id, *(item.id for item in messages_after)]
    _supersede_generations_for_messages(store.db, chat_id, affected_message_ids)
    for later_message in messages_after:
        if later_message.role == "user":
            store.detach_memory_sources_for_message(later_message.id, reason="deletion")
    store.detach_memory_sources_for_message(message_id, reason="replacement")
    message.content = cleaned
    store.db.execute(
        delete(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.id > message_id,
        )
    )
    generation = _start_chat_generation(
        request,
        store,
        chat_id,
        payload,
        user_message_id=message_id,
    )
    return ChatGenerationStartResponse(generation=_generation_read(generation))


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(chat_id: int, store: StoreDependency) -> Response:
    _get_required_chat(store, chat_id)
    for message in store.list_chat_messages(chat_id):
        if message.role == "user":
            store.detach_memory_sources_for_message(message.id, reason="deletion")
    store.delete_chat(chat_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/goals", response_model=list[GoalRead])
def list_goals(store: StoreDependency) -> list[GoalRead]:
    return [GoalRead.model_validate(goal) for goal in store.list_goals(GoalStatus.ACTIVE)]


@router.get("/education", response_model=list[EducationRead])
def list_education(store: StoreDependency) -> list[EducationRead]:
    return [EducationRead.model_validate(item) for item in store.list_education(active_only=True)]


@router.get("/activities", response_model=list[ActivityRead])
def list_activities(store: StoreDependency) -> list[ActivityRead]:
    store.archive_expired_activities()
    store.db.commit()
    return [ActivityRead.model_validate(item) for item in store.list_activities(active_only=True)]


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
        ProjectRead.model_validate(project) for project in store.list_projects(ProjectStatus.ACTIVE)
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


@router.get("/chat-projects", response_model=list[ProjectRead])
def list_chat_projects(store: StoreDependency) -> list[ProjectRead]:
    return list_projects(store)


@router.post("/chat-projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_chat_project(request: ProjectCreateRequest, store: StoreDependency) -> ProjectRead:
    return create_project(request, store)


@router.patch("/chat-projects/{project_id}", response_model=ProjectRead)
def update_chat_project(
    project_id: int,
    request: ProjectUpdateRequest,
    store: StoreDependency,
) -> ProjectRead:
    return update_project(project_id, request, store)


@router.delete("/chat-projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_project(project_id: int, store: StoreDependency) -> Response:
    return delete_project(project_id, store)


@router.delete("/chat-projects/{project_id}/memory", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_project_memory(project_id: int, store: StoreDependency) -> Response:
    return delete_project_memory(project_id, store)


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


@router.get("/memory", response_model=list[MemoryRead])
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
    return [PreferenceRead.model_validate(preference) for preference in store.list_preferences()]


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
