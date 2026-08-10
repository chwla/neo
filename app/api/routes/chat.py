from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import get_store
from app.api.routes.accounts import session_for
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models import Chat, ChatGeneration, ChatMessage, Project
from app.models.enums import ProjectStatus
from app.models.memory import MemoryRecord, MemorySource
from app.repositories.app_store import AppStore
from app.schemas.chat import ProjectRead
from app.services.chat import NeoChatService
from app.services.llm import LLMClient, LLMRegistry, ProviderUsagePersistenceError, get_llm_client
from app.services.llm_registry.providers import ProviderConfigurationError
from app.services.memory.contracts import DetachMemorySourceCommand, TargetRevision
from app.services.memory.factory import build_memory_runtime
from app.services.memory_chat import build_chat_memory_runtime
from app.services.profile_accounts import database_identity_for_profile, profile_database
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
PROCESS_WORKER_ID = str(uuid.uuid4())
GENERATION_LEASE_SECONDS = 120
_GENERATION_THREADS: set[str] = set()
_GENERATION_THREADS_LOCK = Lock()
_GENERATION_LOG = logging.getLogger("neo.chat.generation")


def _llm_client(config_id: str | None = None, route_name: str = "chat") -> LLMClient:
    try:
        return get_llm_client(config_id, route_name=route_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _chat_failure(exc: Exception, config_id: str | None = None) -> tuple[int, str, str]:
    if isinstance(exc, (requests.RequestException, ProviderConfigurationError)):
        # This runs on the failure path, so it must never raise on its own.
        # ``get`` rejects an unknown or disabled configuration, which would
        # otherwise mask the real provider error and leave the generation row
        # stuck without a terminal status.
        try:
            config = LLMRegistry().get(config_id)
        except Exception:
            return (
                502,
                "Provider failed",
                f"The provider did not finish the response. Details: {exc}",
            )
        return (
            502,
            "Provider failed",
            (
                f"{config.name} did not finish the response. Expected {config.model} "
                f"at {config.base_url} within {config.timeout_seconds} seconds. "
                f"Details: {exc}"
            ),
        )
    if isinstance(exc, ProviderUsagePersistenceError):
        return (
            500,
            "Neo persistence failed",
            f"Neo could not persist provider usage data. Details: {exc}",
        )
    return 500, "Chat failed", f"Neo chat failed internally. Details: {exc}"


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
    memory_enabled: bool = True
    memory_incognito: bool = False

    @field_validator("prompt")
    @classmethod
    def require_nonblank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt_must_not_be_blank")
        return value

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
    memory_enabled: bool = True
    memory_incognito: bool = False

    @field_validator("content")
    @classmethod
    def require_nonblank_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content_must_not_be_blank")
        return value


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


class ProjectUpdateRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    priority: int = Field(ge=1, le=10)


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def _get_required_chat(store: AppStore, chat_id: int):
    chat = store.get_chat(chat_id)
    if chat is None or chat.archived:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


def _get_required_project(store: AppStore, project_id: int):
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _thread_payload(store: AppStore, chat_id: int) -> ChatThreadRead:
    chat = _get_required_chat(store, chat_id)
    messages = store.list_chat_messages(chat_id)
    return ChatThreadRead(
        chat=ChatRead.model_validate(chat),
        messages=[_chat_message_read(message) for message in messages],
    )


def _detach_sources_for_message(runtime, db, message_id: int, *, reason: str) -> list[dict]:
    rows = db.execute(
        select(MemorySource, MemoryRecord.revision)
        .join(
            MemoryRecord,
            and_(
                MemoryRecord.owner_id == MemorySource.owner_id,
                MemoryRecord.id == MemorySource.memory_id,
            ),
        )
        .where(
            MemorySource.owner_id == runtime.execution.owner_id,
            MemorySource.message_id == str(message_id),
        )
    ).all()
    results = []
    for source, revision in rows:
        result = runtime.adapter.detach_source(
            runtime.context(
                source_kind=source.source_kind,
                source_id=source.source_id,
                request_id=f"source-change:{message_id}:{source.id}",
                conversation_id=source.conversation_id,
                message_id=str(message_id),
            ),
            DetachMemorySourceCommand(
                owner_id=runtime.execution.owner_id,
                idempotency_key=f"source-change:{message_id}:{source.id}:{reason}",
                target=TargetRevision(memory_id=source.memory_id, expected_revision=revision),
                source_id=source.id,
                detachment_reason=reason,
            ),
        )
        results.append(result.model_dump(mode="json"))
    return results


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


def _generation_service(
    db,
    chat: Chat,
    llm_id: str | None,
    *,
    memory_enabled: bool,
    memory_incognito: bool,
) -> NeoChatService:
    rule_result = RuleResolver().resolve(
        RuleResolveRequest(
            context_type="chat",
            context_id=str(chat.id),
            project_id=str(chat.project_id) if chat.project_id is not None else None,
        )
    )
    route_name = RuleResolver.route_name(rule_result, "chat", "chat")
    return _chat_service(
        db,
        db.info.get("neo_authenticated_profile"),
        request_id=f"generation:{chat.id}",
        ollama=_llm_client(llm_id, route_name),
        rule_result=rule_result,
        memory_enabled=memory_enabled,
        memory_incognito=memory_incognito,
        active_project_id=str(chat.project_id) if chat.project_id is not None else None,
    )


def _chat_service(
    db,
    profile: dict | None,
    *,
    request_id: str,
    ollama: LLMClient,
    rule_result: dict,
    memory_enabled: bool = True,
    memory_incognito: bool = False,
    active_project_id: str | None = None,
) -> NeoChatService:
    runtime = None
    mutation_runtime = None
    settings = get_settings()
    memory_active = bool(
        profile is not None
        and settings.memory_enabled
        and not settings.memory_incognito
        and memory_enabled
        and not memory_incognito
    )
    if memory_active and profile is not None:
        profile_id = str(profile["id"])
        is_guest = bool(profile.get("is_guest"))
        mutation_runtime = build_memory_runtime(profile)
        runtime = build_chat_memory_runtime(
            db,
            owner_id=str(profile["owner_id"]),
            database_identity=database_identity_for_profile(profile_id, guest=is_guest),
            profile_id=profile_id,
            request_id=request_id,
            session_id=(request_id.split(":", 2)[1] if ":" in request_id else request_id),
            memory_enabled=True,
            incognito=False,
            active_project_id=active_project_id,
        )
    return NeoChatService(
        db,
        ollama=ollama,
        rule_result=rule_result,
        memory_orchestrator=runtime.orchestrator if runtime is not None else None,
        memory_context_factory=runtime.context_factory if runtime is not None else None,
        memory_runtime=mutation_runtime,
        active_project_id=active_project_id,
        active_project_name=(
            db.scalar(select(Project.name).where(Project.id == int(active_project_id)))
            if active_project_id is not None
            else None
        ),
    )


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
        # Captured as a plain value so the failure handler below never has to
        # touch an ORM instance that may be unbound (the claim itself raised),
        # rebound to ``None``, or expired by the rollback it performs first.
        llm_id: str | None = None
        try:
            generation = _claim_generation(db, generation_id, lease_token)
            if generation is None:
                return
            llm_id = generation.llm_id
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

            db.info["neo_authenticated_profile"] = profile
            generation_options = json.loads(generation.metadata_json or "{}")
            service = _generation_service(
                db,
                chat,
                generation.llm_id,
                memory_enabled=bool(generation_options.get("memory_enabled", True)),
                memory_incognito=bool(generation_options.get("memory_incognito", False)),
            )
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
            _GENERATION_LOG.exception(
                "Chat generation %s failed for profile %s",
                generation_id,
                profile.get("id"),
            )
            _status_code, status_detail, error = _chat_failure(exc, llm_id)
            _update_leased_generation(
                db,
                generation_id,
                lease_token,
                status="failed",
                status_detail=status_detail,
                error=error,
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
    store: AppStore,
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
    store: AppStore,
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
        metadata_json=json.dumps(
            {
                "memory_enabled": payload.memory_enabled,
                "memory_incognito": payload.memory_incognito,
            }
        ),
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
    http_request: Request,
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
    service = _chat_service(
        store.db,
        session_for(http_request),
        request_id=f"chat:{chat_id}:{uuid.uuid4()}",
        ollama=_llm_client(request.llm_id, route_name),
        rule_result=rule_result,
        memory_enabled=request.memory_enabled,
        memory_incognito=request.memory_incognito,
        active_project_id=str(chat.project_id) if chat.project_id is not None else None,
    )
    try:
        reply = service.send_message(
            chat_id,
            request.prompt,
            timezone=request.timezone,
            locale=request.locale,
        )
    except Exception as exc:
        status_code, _status_detail, detail = _chat_failure(exc, request.llm_id)
        raise HTTPException(status_code=status_code, detail=detail) from exc
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
    http_request: Request,
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
    service = _chat_service(
        store.db,
        session_for(http_request),
        request_id=f"chat-stream:{chat_id}:{uuid.uuid4()}",
        ollama=_llm_client(request.llm_id, route_name),
        rule_result=rule_result,
        memory_enabled=request.memory_enabled,
        memory_incognito=request.memory_incognito,
        active_project_id=str(chat.project_id) if chat.project_id is not None else None,
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
            _status_code, _status_detail, detail = _chat_failure(exc, request.llm_id)
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "detail": detail,
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
    http_request: Request,
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
        settings = get_settings()
        profile = session_for(http_request)
        if (
            profile is not None
            and settings.memory_enabled
            and not settings.memory_incognito
            and request.memory_enabled
            and not request.memory_incognito
        ):
            runtime = build_memory_runtime(profile)
            _detach_sources_for_message(runtime, store.db, message_id, reason="replacement")
        message = store.update_chat_message_content(message_id, cleaned)
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
    settings = get_settings()
    profile = session_for(request)
    memory_active = bool(
        profile is not None
        and settings.memory_enabled
        and not settings.memory_incognito
        and payload.memory_enabled
        and not payload.memory_incognito
    )
    if memory_active:
        runtime = build_memory_runtime(profile)
        for later_message in messages_after:
            if later_message.role == "user":
                _detach_sources_for_message(runtime, store.db, later_message.id, reason="deletion")
        _detach_sources_for_message(runtime, store.db, message_id, reason="replacement")
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
def delete_chat(
    chat_id: int,
    request: Request,
    store: StoreDependency,
    memory_enabled: bool = True,
    memory_incognito: bool = False,
) -> Response:
    _get_required_chat(store, chat_id)
    settings = get_settings()
    profile = session_for(request)
    if (
        profile is not None
        and settings.memory_enabled
        and not settings.memory_incognito
        and memory_enabled
        and not memory_incognito
    ):
        runtime = build_memory_runtime(profile)
        for message in store.list_chat_messages(chat_id):
            if message.role == "user":
                _detach_sources_for_message(runtime, store.db, message.id, reason="deletion")
    store.delete_chat(chat_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _list_chat_projects(store: StoreDependency) -> list[ProjectRead]:
    return [
        ProjectRead.model_validate(project) for project in store.list_projects(ProjectStatus.ACTIVE)
    ]


def _create_chat_project(request: ProjectCreateRequest, store: StoreDependency) -> ProjectRead:
    cleaned = " ".join(request.name.split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="Project name is required")
    project = store.create_project(cleaned)
    store.db.commit()
    store.db.refresh(project)
    return ProjectRead.model_validate(project)


def _update_chat_project(
    project_id: int,
    request: ProjectUpdateRequest,
    store: StoreDependency,
) -> ProjectRead:
    _get_required_project(store, project_id)
    store.update_project(
        project_id,
        request.name.strip(),
        _clean_optional_text(request.description),
        request.priority,
    )
    store.db.commit()
    project = store.get_project(project_id)
    return ProjectRead.model_validate(project)


def _delete_chat_project(project_id: int, store: StoreDependency) -> Response:
    _get_required_project(store, project_id)
    store.delete_project(project_id)
    store.db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chat-projects", response_model=list[ProjectRead])
def list_chat_projects(store: StoreDependency) -> list[ProjectRead]:
    return _list_chat_projects(store)


@router.post("/chat-projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_chat_project(request: ProjectCreateRequest, store: StoreDependency) -> ProjectRead:
    return _create_chat_project(request, store)


@router.patch("/chat-projects/{project_id}", response_model=ProjectRead)
def update_chat_project(
    project_id: int,
    request: ProjectUpdateRequest,
    store: StoreDependency,
) -> ProjectRead:
    return _update_chat_project(project_id, request, store)


@router.delete("/chat-projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_project(project_id: int, store: StoreDependency) -> Response:
    return _delete_chat_project(project_id, store)
