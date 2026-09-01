from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from threading import Lock, Thread
from typing import Annotated, Literal
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
from app.services import chat_events
from app.services.agent_core import events as agent_events
from app.services.chat import NeoChatService
from app.services.llm import (
    LLMClient,
    LLMMessage,
    LLMRegistry,
    ProviderUsagePersistenceError,
    get_llm_client,
)
from app.services.llm_registry.providers import ProviderConfigurationError
from app.services.memory.contracts import DetachMemorySourceCommand, TargetRevision
from app.services.memory.factory import build_memory_runtime
from app.services.memory_chat import build_chat_memory_runtime
from app.services.profile_accounts import database_identity_for_profile, profile_database
from app.services.provider_runtime.errors import (
    ContextTooLargeError,
    ProviderFailure,
    classify,
    user_message,
)
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleResolveRequest

router = APIRouter()
StoreDependency = Annotated[AppStore, Depends(get_store)]
PROCESS_WORKER_ID = str(uuid.uuid4())
GENERATION_LEASE_SECONDS = 120
_GENERATION_THREADS: set[str] = set()
_GENERATION_THREADS_LOCK = Lock()
_GENERATION_LOG = logging.getLogger("neo.chat.generation")
#: Where a failed send records what actually went wrong. The response carries only
#: the safe sentence, so this is the only place the provider's own text survives.
_CHAT_LOG = logging.getLogger("neo.chat")


def _llm_client(config_id: str | None = None, route_name: str = "chat") -> LLMClient:
    try:
        return get_llm_client(config_id, route_name=route_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


#: HTTP status per provider failure category. A provider that is simply not answering is
#: 503 rather than 502: nothing served the request, and the client may retry it as it is.
_FAILURE_STATUS = {
    "provider_unavailable": 503,
    "timeout": 504,
    "transient_network": 503,
    "rate_limited": 429,
    "auth_or_config": 502,
    "unsupported_capability": 502,
}
_FAILURE_HEADLINE = {
    "provider_unavailable": "Model unavailable",
    "timeout": "Model timed out",
    "transient_network": "Model unreachable",
    "rate_limited": "Rate limited",
    "auth_or_config": "Model not configured",
    "unsupported_capability": "Model unsupported",
}


def _chat_failure(exc: Exception, config_id: str | None = None) -> tuple[int, str, str]:
    """Reduce any send failure to a status, a short headline, and one safe sentence.

    Nothing derived from the exception's own text is returned. A provider error carries
    the host, port and urllib3 frames that produced it, and every one of these three
    values is rendered verbatim by the UI, so the sentence is chosen from the failure's
    category instead. The exception itself goes to the log, where it belongs.
    """

    if isinstance(exc, ContextTooLargeError):
        # The message is simply bigger than the model can read. That is a limit the user
        # can act on, not an internal fault, so it must not read as one. The text is
        # Neo's own sentence about token counts, not the provider's.
        return 413, "Message too long", str(exc)
    if isinstance(exc, ProviderFailure):
        _CHAT_LOG.warning(
            "chat_provider_failure category=%s provider=%s detail=%s",
            exc.category,
            exc.provider or "unknown",
            exc.detail or str(exc),
        )
        return (
            _FAILURE_STATUS.get(exc.category, 502),
            _FAILURE_HEADLINE.get(exc.category, "Provider failed"),
            str(exc),
        )
    if isinstance(exc, (requests.RequestException, ProviderConfigurationError)):
        # A provider reached directly, outside the runtime client. Classify it the same
        # way so the user reads the same sentence either way.
        category = classify(exc)
        provider = _provider_type_for(config_id)
        _CHAT_LOG.warning(
            "chat_provider_failure category=%s provider=%s", category, provider, exc_info=exc
        )
        return (
            _FAILURE_STATUS.get(category, 502),
            _FAILURE_HEADLINE.get(category, "Provider failed"),
            user_message(category, provider),
        )
    if isinstance(exc, ProviderUsagePersistenceError):
        _CHAT_LOG.exception("chat_usage_persistence_failed")
        return (
            500,
            "Neo persistence failed",
            "Neo could not record this response. Try again.",
        )
    _CHAT_LOG.exception("chat_failed_unexpectedly")
    return 500, "Chat failed", "Neo could not finish this response. Try again."


def _provider_type_for(config_id: str | None) -> str:
    """The provider's type, for wording only — never its name, URL, or model.

    This runs on the failure path, so it must never raise on its own: an unknown or
    disabled configuration would otherwise mask the provider error being reported.
    """

    try:
        return LLMRegistry().get(config_id).provider
    except Exception:
        return ""


class ChatRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    project_id: int | None
    archived: bool
    pinned: bool = False
    #: What an agent turn in this chat runs against. The workspace and the
    #: permission mode belong to the conversation, not to one message.
    repo_id: str | None = None
    agent_mode: str = "normal"
    agent_definition_id: str | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    #: Set only while a run in this chat is unfinished, so the sidebar can badge
    #: the row without a second request. A chat whose agent turns are all done
    #: is an ordinary chat again.
    agent_status: str | None = None
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
    #: Present on `response_kind == "agent_run"` rows: the run this turn is,
    #: with everything the transcript needs to draw it on first paint rather
    #: than after a second request per turn.
    agent: dict[str, object] | None = None
    created_at: datetime


class ProjectWithChatsRead(ProjectRead):
    chats: list[ChatRead] = Field(default_factory=list)


class SidebarRead(BaseModel):
    """Every thread, in one list.

    Agent runs used to be carried separately because they had their own view to
    open. Now a run is a turn of a chat, so it appears wherever that chat does
    and its status rides on ``ChatRead.agent_status``.
    """

    projects: list[ProjectWithChatsRead]
    chats: list[ChatRead]


class ChatThreadRead(BaseModel):
    chat: ChatRead
    messages: list[ChatMessageRead]
    #: The sequence number to tail the chat's live log from. The server decides
    #: it because only the server knows whether a turn is still in flight, and a
    #: browser guessing wrong either replays the whole thread or misses the
    #: part of a running turn it did not see.
    stream_after: int = 0


class ChatCreateRequest(BaseModel):
    project_id: int | None = None


class ChatForkRequest(BaseModel):
    #: The message to branch from -- this message and everything before it is
    #: copied into the new chat.
    message_id: int


class ChatCompactRequest(BaseModel):
    llm_id: str | None = Field(default=None, max_length=80)


class ChatCompactResponse(BaseModel):
    chat: ChatRead
    summary_message: ChatMessageRead | None = None
    compacted_message_count: int
    kept_message_count: int


class ChatSendRequest(BaseModel):
    prompt: str = Field(min_length=1)
    llm_id: str | None = Field(default=None, max_length=80)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=80)
    timezone: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=40)
    memory_enabled: bool = True
    memory_incognito: bool = False
    #: Gallery items shown to the model with this message. Ids rather than bytes:
    #: the image is already stored, and the whole point of the gallery is that it
    #: is not uploaded again on every turn that refers to it.
    image_ids: list[str] = Field(default_factory=list, max_length=8)
    #: Which kind of turn this message starts. Chosen per message, defaulting to
    #: whatever the chat was last set to, so a thread can answer a question and
    #: then go and do the work without changing where the user is.
    mode: Literal["chat", "agent"] | None = None
    #: Overrides for the chat's agent settings. Sent when the composer's chips
    #: changed in the same gesture as the message, and persisted onto the chat
    #: so the next turn inherits them.
    repo_id: str | None = Field(default=None, max_length=64)
    agent_mode: Literal["plan", "normal", "auto"] | None = None
    agent_definition_id: str | None = Field(default=None, max_length=64)

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


class ChatUpdateRequest(BaseModel):
    """A rename, a pin, an agent setting, or any combination.

    Every field is optional so one can move alone: the composer's chips patch a
    single setting without having to restate the title it is not changing.
    """

    title: str | None = Field(default=None, min_length=1, max_length=120)
    pinned: bool | None = None
    repo_id: str | None = Field(default=None, max_length=64)
    agent_mode: Literal["plan", "normal", "auto"] | None = None
    agent_definition_id: str | None = Field(default=None, max_length=64)
    disabled_tools: list[str] | None = None

    @field_validator("title")
    @classmethod
    def require_nonblank_title(cls, value: str | None) -> str | None:
        # A title of only whitespace collapses to nothing in the sidebar, so it is
        # rejected the same way a blank prompt is.
        if value is not None and not value.strip():
            raise ValueError("title_must_not_be_blank")
        return value


class ChatToolCatalogEntry(BaseModel):
    """One tool as offered to this chat's agent, whether built in or bridged
    from a connector definition -- see ``agent_core.tools.connectors``."""

    name: str
    display_name: str
    description: str
    risk: str
    category: str
    requires_repo: bool
    source: Literal["built_in", "connector"]
    enabled: bool
    tool_id: str | None = None
    server_name: str | None = None
    group: str | None = None


class ChatToolsRead(BaseModel):
    tools: list[ChatToolCatalogEntry]


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
    #: Absent for an agent turn, which has no generation row -- its durable
    #: state is the session, and the anchor row below is its place in the chat.
    generation: ChatGenerationRead | None = None
    #: Set when the turn was an agent run. The browser needs both: the anchor to
    #: know which row in the transcript is filling in, and the session to send
    #: approvals, steering and delivery to.
    agent_session_id: str | None = None
    anchor_message_id: int | None = None


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
    runs = _agent_runs_by_anchor(chat_id)
    active = next(
        (run for run in runs.values() if run["session"]["status"] in _ACTIVE_AGENT_STATUSES),
        None,
    )
    return ChatThreadRead(
        chat=ChatRead.model_validate(chat).model_copy(
            update={"agent_status": active["session"]["status"] if active else None}
        ),
        messages=[_chat_message_read(message, runs.get(message.id)) for message in messages],
        stream_after=_stream_cursor(store, chat_id, active),
    )


_ACTIVE_AGENT_STATUSES = frozenset({"queued", "running", "waiting_approval"})


def _agent_runs_by_anchor(chat_id: int) -> dict[int, dict[str, object]]:
    """Every agent turn in this chat, keyed by the row that holds its place.

    Read here rather than per message so a long thread costs one query instead
    of one per turn, and so a transcript arrives complete: the trace, the
    checklist and any pending approval are all on the first paint, which is what
    lets a reopened chat show a waiting run without a second request.

    The run transcript itself is deliberately left out. The anchor row already
    carries what the agent said; the model-facing messages are the run's
    working state, not the conversation.
    """

    try:
        from app.services.agent_core import store as agent_store
        from app.services.agent_core.service import AgentCoreService

        sessions = agent_store.sessions_for_chat(chat_id)
    except Exception:
        return {}
    service = AgentCoreService()
    runs: dict[int, dict[str, object]] = {}
    for row in sessions:
        anchor_id = row.get("anchor_message_id")
        if not anchor_id:
            continue
        try:
            detail = service.detail(str(row["id"]))
        except Exception:
            continue
        runs[int(anchor_id)] = {
            "session": detail["session"],
            "tool_calls": detail["tool_calls"],
            "pending_approval": detail["pending_approval"],
            "grants": detail["grants"],
            "delivery": detail["delivery"],
        }
    return runs


def _stream_cursor(store: AppStore, chat_id: int, active: dict[str, object] | None) -> int:
    """Where a reader should start tailing this chat.

    A finished thread is fully described by its message rows, so the tail starts
    at the end and replays nothing. A thread with a turn still running starts
    just before that turn's first event instead, so reopening a chat mid-run
    rebuilds the narration and tool cards produced while nobody was watching.
    """

    if active is not None:
        first = chat_events.first_seq_for(
            chat_id, agent_session_id=str(active["session"]["id"])
        )
        if first:
            return first - 1
    generation = store.db.scalar(
        select(ChatGeneration)
        .where(ChatGeneration.chat_id == chat_id, ChatGeneration.status.in_(("queued", "running")))
        .order_by(ChatGeneration.created_at.desc())
    )
    if generation is not None:
        first = chat_events.first_seq_for(chat_id, generation_id=generation.id)
        if first:
            return first - 1
    return chat_events.latest_seq(chat_id)


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


def _chat_message_read(
    message: ChatMessage,
    agent: dict[str, object] | None = None,
) -> ChatMessageRead:
    metadata = _json_object(message.metadata_json)
    search_trace = metadata.get("search_trace") or metadata.get("web_debug") or {}
    connector_trace = metadata.get("connector_trace") or {}
    payload = {
        field: getattr(message, field)
        for field in ChatMessageRead.model_fields
        if field not in {"metadata", "search_trace", "connector_trace", "agent"}
    }
    return ChatMessageRead.model_validate(
        {
            **payload,
            "metadata": metadata,
            "search_trace": search_trace if isinstance(search_trace, dict) else {},
            "connector_trace": connector_trace if isinstance(connector_trace, dict) else {},
            "agent": agent,
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
    image_ids: list[str] | None = None,
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
        image_ids=image_ids,
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
    image_ids: list[str] | None = None,
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
        image_ids=image_ids,
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


def _persist_cancelled_partial(
    db, generation_id: str, partial_response: str, thinking: str
) -> None:
    """Save a stopped response so the user keeps what had already been written."""

    generation = db.get(ChatGeneration, generation_id)
    if generation is None or generation.status != "cancelled":
        return
    if generation.assistant_message_id or not partial_response.strip():
        return
    message = ChatMessage(
        chat_id=generation.chat_id,
        role="assistant",
        content=partial_response,
        thinking=thinking or None,
        finish_reason="cancelled",
        generation_id=generation.id,
        provider_name=generation.provider_name,
        model_name=generation.model_name,
    )
    db.add(message)
    db.flush()
    generation.assistant_message_id = message.id
    generation.reply = partial_response
    generation.partial_response = partial_response
    db.commit()


def _chat_id_for_generation(db, generation_id: str) -> int | None:
    """Read a generation's chat without trusting an ORM instance to still be bound."""

    try:
        return db.scalar(
            select(ChatGeneration.chat_id).where(ChatGeneration.id == generation_id)
        )
    except Exception:  # pragma: no cover - the session is already failing
        return None


class _ChatEventEmitter:
    """Publish a generation's progress into the chat's live log.

    A reply arrives token by token, and a row per token would turn the log into
    the transcript's largest table for no reader's benefit -- the browser
    repaints far slower than the provider emits.  Deltas are therefore buffered
    and flushed on a short interval, and always ahead of any event whose meaning
    depends on the text before it.

    The generation row remains the durable record.  This log is how a reader
    watches, so a failure to write one is never allowed to reach the worker.
    """

    FLUSH_SECONDS = 0.12

    def __init__(self, chat_id: int, generation_id: str) -> None:
        self.chat_id = chat_id
        self.generation_id = generation_id
        self._pending = ""
        self._last_flush = 0.0

    def _append(self, event_type: str, payload: dict | None = None) -> None:
        try:
            chat_events.append(
                self.chat_id,
                event_type,
                payload,
                generation_id=self.generation_id,
            )
        except Exception:  # pragma: no cover - watching must not break generating
            _GENERATION_LOG.debug("chat event dropped for generation %s", self.generation_id)

    def chunk(self, text: str) -> None:
        if not text:
            return
        self._pending += text
        if time.monotonic() - self._last_flush >= self.FLUSH_SECONDS:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        pending, self._pending = self._pending, ""
        self._last_flush = time.monotonic()
        self._append(agent_events.CHUNK, {"content": pending})

    def event(self, event_type: str, payload: dict | None = None) -> None:
        """Emit something that is only correct after the text preceding it."""

        self.flush()
        self._append(event_type, payload)


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
                image_ids=list(generation_options.get("image_ids") or []),
            )
            partial_response = ""
            thinking = ""
            emitter = _ChatEventEmitter(chat.id, generation_id)
            emitter.event(agent_events.RUN_STARTED, {"objective": generation.prompt})
            for event in service.stream_message(
                chat.id,
                generation.prompt,
                existing_user_message_id=generation.user_message_id,
                timezone=generation.timezone,
                locale=generation.locale,
                generation_id=generation.id,
                generation_lease_token=lease_token,
                # The user's clock starts when the turn was accepted, not when a
                # worker got to it, so a turn that waited its way through the
                # queue reports the wait the composer showed.
                turn_started_at=generation.created_at,
            ):
                values: dict[str, object] = {"heartbeat_at": datetime.now(UTC)}
                if event["type"] == "chunk":
                    partial_response += str(event.get("content") or "")
                    values["partial_response"] = partial_response
                    emitter.chunk(str(event.get("content") or ""))
                elif event["type"] == "thinking":
                    thinking += str(event.get("content") or "")
                    values["thinking"] = thinking
                    emitter.event(agent_events.THINKING, {"content": event.get("content") or ""})
                elif event["type"] == "replace":
                    partial_response = str(event.get("content") or "")
                    values["partial_response"] = partial_response
                    emitter.event(agent_events.REPLACE, {"content": partial_response})
                elif event["type"] == "status":
                    values["status_detail"] = str(event.get("content") or "")[:120] or None
                    emitter.event(agent_events.STATUS, {"content": event.get("content") or ""})
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
                    emitter.event(
                        agent_events.RUN_COMPLETED,
                        {
                            "message_id": event.get("message_id"),
                            "reply": reply,
                            "thinking": str(event.get("thinking") or thinking) or None,
                            "response_kind": event.get("response_kind"),
                            "provider_name": event.get("provider_name") or event.get("provider"),
                            "model_name": event.get("model_name") or event.get("model"),
                            "route_name": event.get("route_name"),
                            "finish_reason": event.get("finish_reason"),
                            "prompt_tokens": event.get("prompt_tokens"),
                            "completion_tokens": event.get("completion_tokens"),
                            "total_tokens": event.get("total_tokens"),
                            "duration_ms": event.get("duration_ms"),
                        },
                    )
                if not _update_leased_generation(
                    db,
                    generation_id,
                    lease_token,
                    **values,
                ):
                    # The lease is gone. If the user stopped this response, keep the text
                    # generated so far instead of discarding it.
                    _persist_cancelled_partial(db, generation_id, partial_response, thinking)
                    emitter.event(agent_events.RUN_CANCELLED, {"reply": partial_response})
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
                emitter.event(
                    agent_events.RUN_FAILED,
                    {"error": "The response ended without a completion event."},
                )
        except Exception as exc:
            db.rollback()
            if isinstance(exc, ContextTooLargeError):
                # A prompt over the limit is a user-facing outcome, not a defect. Logging
                # a traceback for it would bury the failures that do need attention.
                _GENERATION_LOG.info(
                    "Chat generation %s exceeded the model context: %s", generation_id, exc
                )
            elif isinstance(exc, ProviderFailure):
                # The provider runtime already logged this one with its original
                # traceback; a stopped Ollama is not a defect in the worker.
                _GENERATION_LOG.info(
                    "Chat generation %s ended on a provider failure (%s): %s",
                    generation_id,
                    exc.category,
                    exc.detail or exc,
                )
            else:
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
            # A reader tailing this chat is waiting on a terminal event, and the
            # failure above is one. Without it the tail would sit until its idle
            # timeout before the browser learned anything went wrong.
            failed_chat_id = _chat_id_for_generation(db, generation_id)
            if failed_chat_id is not None:
                _ChatEventEmitter(failed_chat_id, generation_id).event(
                    agent_events.RUN_FAILED, {"error": error}
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
    cleaned_prompt = payload.prompt.strip()
    if payload.client_request_id:
        # Same key space as the synchronous path, so a key cannot be spent twice across
        # the two, and scoped like the unique index rather than by chat.
        existing = _generation_for_client_request(store, payload.client_request_id)
        if existing is not None:
            _require_matching_claim(existing, chat.id, cleaned_prompt, payload.client_request_id)
            _recover_generation(request, store, existing)
            return existing
    generation_id = str(uuid.uuid4())
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
                "image_ids": payload.image_ids,
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
            # Match the unique index, which spans the profile database rather than one
            # chat. A chat-scoped lookup here missed the blocking row and re-raised.
            existing = _generation_for_client_request(store, payload.client_request_id)
            if existing is not None:
                _require_matching_claim(
                    existing, chat.id, cleaned_prompt, payload.client_request_id
                )
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
    running = _active_agent_status_by_chat()
    projects = []
    for project in store.list_projects(ProjectStatus.ACTIVE):
        chats = store.list_chats(project_id=project.id, with_messages_only=True, limit=12)
        project_data = ProjectRead.model_validate(project).model_dump()
        projects.append(
            ProjectWithChatsRead(
                **project_data,
                chats=[_sidebar_chat_read(chat, running) for chat in chats],
            )
        )
    chats = store.list_chats(unprojected_only=True, with_messages_only=True, limit=20)
    return SidebarRead(
        projects=projects,
        chats=[_sidebar_chat_read(chat, running) for chat in chats],
    )


def _sidebar_chat_read(chat: Chat, running: dict[int, str]) -> ChatRead:
    return ChatRead.model_validate(chat).model_copy(
        update={"agent_status": running.get(chat.id)}
    )


def _active_agent_status_by_chat() -> dict[int, str]:
    """Which chats have a run still going, for the sidebar badge.

    The agent store is a separate SQLite layer from the chat ORM, so a failure to
    read it must not take the whole sidebar down with it -- the chats are still
    the answer, just without their badges.
    """

    try:
        from app.services.agent_core import store as agent_store

        return agent_store.active_status_by_chat()
    except Exception:
        return {}


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


# A generation still doing work must not be replayed as if it had an answer.
_ACTIVE_GENERATION_STATUSES = frozenset({"queued", "running", "streaming"})
# How long a concurrent duplicate waits for the original before giving up on it.
SYNC_IDEMPOTENCY_WAIT_SECONDS = 180


def _generation_for_client_request(store: AppStore, client_request_id: str):
    """Find the row holding this idempotency key.

    Deliberately not scoped by chat: the unique index spans ``client_request_id`` across
    the whole profile database, so a chat-scoped lookup would miss a row that still
    blocks the insert and turn a reused key into an integrity error.
    """
    return store.db.scalar(
        select(ChatGeneration).where(ChatGeneration.client_request_id == client_request_id)
    )


def _require_matching_claim(
    generation: ChatGeneration, chat_id: int, prompt: str, client_request_id: str
) -> None:
    """An idempotency key names one request; reusing it for another is a client error.

    Returning the first request's answer for a different prompt would be worse than
    failing, so this conflicts explicitly rather than replying with unrelated output.
    """
    if generation.chat_id != chat_id or (generation.prompt or "").strip() != prompt:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"client_request_id '{client_request_id}' was already used for a different "
                "prompt or chat in this profile. Send a new client_request_id."
            ),
        )


def _await_terminal_generation(
    store: AppStore,
    generation: ChatGeneration,
    timeout: float = SYNC_IDEMPOTENCY_WAIT_SECONDS,
) -> ChatGeneration:
    """Wait out an in-flight original so a duplicate converges on its result."""
    deadline = time.monotonic() + timeout
    while generation.status in _ACTIVE_GENERATION_STATUSES and time.monotonic() < deadline:
        time.sleep(0.2)
        # Ending the read transaction is what lets the next read observe the commit made
        # by the connection that owns the claim.
        store.db.rollback()
        refreshed = store.db.get(ChatGeneration, generation.id)
        if refreshed is None:
            break
        generation = refreshed
    return generation


def _acquire_sync_claim(
    store: AppStore, chat: Chat, payload: ChatSendRequest, prompt: str
) -> tuple[ChatGeneration | None, ChatGeneration | None]:
    """Claim the key, or hand back the generation that already owns it.

    The claim is committed before any model work begins. That is what makes the unique
    index effective against a concurrent duplicate: the loser of the race sees a
    committed row and reuses it instead of starting a second generation.
    """
    key = payload.client_request_id
    existing = _generation_for_client_request(store, key)
    if existing is not None:
        _require_matching_claim(existing, chat.id, prompt, key)
        return None, existing

    generation = ChatGeneration(
        id=str(uuid.uuid4()),
        chat_id=chat.id,
        prompt=prompt,
        llm_id=payload.llm_id,
        client_request_id=key,
        status="running",
        status_detail="Running",
        timezone=payload.timezone,
        locale=payload.locale,
        metadata_json=json.dumps(
            {
                "memory_enabled": payload.memory_enabled,
                "memory_incognito": payload.memory_incognito,
                "image_ids": payload.image_ids,
                "transport": "sync",
            }
        ),
        worker_id=PROCESS_WORKER_ID,
        attempt_count=1,
        started_at=datetime.now(UTC),
    )
    store.db.add(generation)
    try:
        store.db.commit()
    except IntegrityError:
        # Another request committed the same key first; adopt its row.
        store.db.rollback()
        existing = _generation_for_client_request(store, key)
        if existing is None:
            raise
        _require_matching_claim(existing, chat.id, prompt, key)
        return None, existing
    store.db.refresh(generation)
    return generation, None


def _replay_sync_send(
    store: AppStore, chat_id: int, generation: ChatGeneration, client_request_id: str
) -> ChatSendResponse:
    """Return the original turn's outcome without creating a second one."""
    generation = _await_terminal_generation(store, generation)
    if generation.status in _ACTIVE_GENERATION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A request with client_request_id '{client_request_id}' is still running. "
                "Retry once it finishes."
            ),
        )
    if generation.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                generation.error
                or f"The original request for client_request_id '{client_request_id}' failed."
            ),
        )
    payload = _thread_payload(store, chat_id)
    return ChatSendResponse(
        chat=payload.chat,
        messages=payload.messages,
        reply=generation.reply or "",
        web_debug={},
    )


def _release_sync_claim(store: AppStore, generation: ChatGeneration) -> None:
    """Drop the claim after a failure so the same key may be retried."""
    try:
        store.db.rollback()
        store.db.delete(generation)
        store.db.commit()
    except Exception:  # noqa: BLE001 - never mask the original failure
        store.db.rollback()


def _complete_sync_claim(
    store: AppStore,
    generation: ChatGeneration,
    chat_id: int,
    reply: str,
    baseline_message_id: int,
    client_request_id: str,
) -> None:
    """Record the outcome and tie the new turn to the claim.

    The chat service creates the message rows itself and returns only the reply text, so
    the new turn is identified here by id. This is also what puts ``generation_id`` and
    ``client_request_id`` on a synchronous turn, which previously carried neither.
    """
    created = [
        message for message in store.list_chat_messages(chat_id) if message.id > baseline_message_id
    ]
    user_message = next((item for item in created if item.role == "user"), None)
    assistant_message = next((item for item in reversed(created) if item.role == "assistant"), None)
    if user_message is not None:
        metadata = _json_object(user_message.metadata_json)
        metadata.update({"generation_id": generation.id, "client_request_id": client_request_id})
        user_message.metadata_json = json.dumps(metadata, default=str, sort_keys=True)
        generation.user_message_id = user_message.id
    if assistant_message is not None:
        assistant_message.generation_id = generation.id
        generation.assistant_message_id = assistant_message.id
    generation.status = "completed"
    generation.status_detail = "Completed"
    generation.reply = reply
    generation.completed_at = datetime.now(UTC)
    store.db.commit()


def _latest_message_id(store: AppStore, chat_id: int) -> int:
    messages = store.list_chat_messages(chat_id)
    return messages[-1].id if messages else 0


@router.post("/chats/{chat_id}/messages", response_model=ChatSendResponse)
def send_chat_message(
    chat_id: int,
    request: ChatSendRequest,
    http_request: Request,
    store: StoreDependency,
) -> ChatSendResponse:
    chat = _get_required_chat(store, chat_id)
    cleaned_prompt = request.prompt.strip()

    # A client_request_id makes the send idempotent. The claim is taken and committed
    # before any model work, so a duplicate — whether it arrives after the first
    # finished or concurrently with it — reuses that turn instead of creating another.
    claim: ChatGeneration | None = None
    if request.client_request_id:
        ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
        claim, replay = _acquire_sync_claim(store, chat, request, cleaned_prompt)
        if replay is not None:
            return _replay_sync_send(store, chat_id, replay, request.client_request_id)

    baseline_message_id = _latest_message_id(store, chat_id)
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
        image_ids=request.image_ids,
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
        if claim is not None:
            # A failed send consumes no key, so the client may retry the same one.
            _release_sync_claim(store, claim)
        status_code, _status_detail, detail = _chat_failure(exc, request.llm_id)
        raise HTTPException(status_code=status_code, detail=detail) from exc
    if claim is not None:
        _complete_sync_claim(
            store,
            claim,
            chat_id,
            reply,
            baseline_message_id,
            request.client_request_id,
        )
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
    """Start the next turn, of whichever kind the composer asked for.

    One entry point rather than two because from the thread's point of view the
    user did the same thing either way: they sent a message. What differs is
    what runs, not where it lands.
    """

    if not payload.prompt.strip():
        raise HTTPException(status_code=422, detail="Message content is required")
    chat = _get_required_chat(store, chat_id)
    # A message with no stated kind is a reply, never a run -- it must not start
    # something that edits files because an earlier turn in this chat did. The
    # composer always states its kind; this is the answer for everything that
    # does not, including the CLI and older clients.
    if payload.mode == "agent":
        return _start_agent_turn(store, chat, payload)
    generation = _start_chat_generation(request, store, chat_id, payload)
    return ChatGenerationStartResponse(generation=_generation_read(generation))


def _apply_agent_settings(store: AppStore, chat: Chat, payload: ChatSendRequest) -> None:
    """Persist chips that moved in the same gesture as the message."""

    if payload.repo_id is not None:
        chat.repo_id = payload.repo_id or None
    if payload.agent_mode is not None:
        chat.agent_mode = payload.agent_mode
    if payload.agent_definition_id is not None:
        chat.agent_definition_id = payload.agent_definition_id or None
    store.db.commit()


def _start_agent_turn(
    store: AppStore,
    chat: Chat,
    payload: ChatSendRequest,
) -> ChatGenerationStartResponse:
    """Run the agent as a turn of this chat.

    The user's prompt is persisted the same way any message is, then an empty
    assistant row is written to hold the turn's place while the run works. That
    row is what the transcript draws the trace into and what the finished run
    writes its answer back onto, so an agent turn occupies one position in the
    conversation from the moment it starts rather than appearing at the end.
    """

    from app.services.agent_core import store as agent_store
    from app.services.agent_core.service import (
        AgentCoreService,
        AgentCoreValidationError,
        SessionCreate,
    )

    cleaned_prompt = payload.prompt.strip()
    if payload.client_request_id:
        # Checked before anything is written: a retried submit must not add a
        # second pair of rows to the transcript for one send.
        existing = agent_store.get_session_by_request(payload.client_request_id)
        if existing is not None:
            return ChatGenerationStartResponse(
                agent_session_id=str(existing["id"]),
                anchor_message_id=existing.get("anchor_message_id"),
            )

    _apply_agent_settings(store, chat, payload)
    store.add_chat_message(
        chat.id,
        "user",
        cleaned_prompt,
        metadata={"client_request_id": payload.client_request_id},
    )
    store.rename_chat_from_prompt(chat.id, cleaned_prompt)
    anchor = store.add_chat_message(chat.id, "assistant", "", response_kind="agent_run")
    store.db.commit()

    try:
        session = AgentCoreService().create(
            SessionCreate(
                objective=cleaned_prompt,
                mode=chat.agent_mode or "normal",
                repo_id=chat.repo_id,
                agent_definition_id=chat.agent_definition_id,
                disabled_tools=chat.disabled_tools or [],
                chat_id=chat.id,
                anchor_message_id=anchor.id,
                client_request_id=payload.client_request_id,
            )
        )
    except AgentCoreValidationError as exc:
        # The turn never started, so the placeholder would sit empty forever.
        # Saying why in the row itself keeps the refusal in the conversation
        # where it was asked for.
        anchor.content = str(exc)
        anchor.response_kind = "agent_error"
        store.db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    anchor.metadata_json = json.dumps({"agent_session_id": session.id}, sort_keys=True)
    store.db.commit()
    return ChatGenerationStartResponse(
        agent_session_id=session.id,
        anchor_message_id=anchor.id,
    )


#: How long a chat tail waits for new events before closing. The browser
#: reconnects with its last sequence number, so a close costs nothing and keeps
#: a stalled connection from holding a worker thread forever.
CHAT_STREAM_IDLE_TIMEOUT = 90.0
CHAT_STREAM_POLL_INTERVAL = 0.25


@router.get("/chats/{chat_id}/events")
def stream_chat_events(
    chat_id: int,
    store: StoreDependency,
    after: int = 0,
) -> StreamingResponse:
    """Tail one chat's live log as newline-delimited JSON.

    This is the single live transport for a thread, whichever kind of turn is
    producing: the generation worker and the agent loop both append here, so a
    browser watching a conversation that answers a question and then goes and
    edits files holds one connection with one cursor.

    Streaming and resumption are the same mechanism, as they already were for a
    run: the log is append-only with a monotonic sequence, so a reload asks for
    everything after the last sequence it saw and misses nothing.
    """

    _get_required_chat(store, chat_id)

    def generate():
        cursor = max(0, after)
        idle_since = time.monotonic()
        while True:
            batch = chat_events.list_events(chat_id, after=cursor)
            for event in batch:
                cursor = event["seq"]
                yield json.dumps(event, default=str) + "\n"
                if event["type"] in agent_events.TERMINAL_EVENTS:
                    return
            if batch:
                idle_since = time.monotonic()
                continue
            if not chat_events.has_active_turn(chat_id):
                # Nothing is generating, so nothing will arrive. A chat has no
                # terminal state of its own; this stands in for one.
                yield json.dumps({"type": "idle", "seq": cursor}) + "\n"
                return
            if time.monotonic() - idle_since > CHAT_STREAM_IDLE_TIMEOUT:
                yield json.dumps({"type": "idle", "seq": cursor}) + "\n"
                return
            time.sleep(CHAT_STREAM_POLL_INTERVAL)

    return StreamingResponse(generate(), media_type="application/x-ndjson")


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


@router.post(
    "/chats/{chat_id}/generations/{generation_id}/cancel", response_model=ChatGenerationRead
)
def cancel_chat_generation(
    chat_id: int,
    generation_id: str,
    store: StoreDependency,
) -> ChatGenerationRead:
    """Stop an in-flight response.

    Flipping the row out of "running" is enough: every worker write is guarded by a lease
    that requires that status, so the worker gives up at its next event rather than being
    killed mid-write.
    """
    _get_required_chat(store, chat_id)
    ChatGeneration.__table__.create(bind=store.db.get_bind(), checkfirst=True)
    generation = store.db.get(ChatGeneration, generation_id)
    if generation is None or generation.chat_id != chat_id:
        raise HTTPException(status_code=404, detail="Chat generation not found")
    if generation.status in {"queued", "running"}:
        generation.status = "cancelled"
        generation.status_detail = "Stopped"
        generation.completed_at = datetime.now(UTC)
        store.db.commit()
        store.db.refresh(generation)
        # The worker will notice at its next event, but it may be blocked on the
        # provider for a while yet. Saying so here is what makes Stop feel like
        # it stopped something.
        chat_events.append(
            chat_id,
            agent_events.RUN_CANCELLED,
            {"reply": generation.partial_response or ""},
            generation_id=generation.id,
        )
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
        image_ids=request.image_ids,
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


@router.post("/chats/{chat_id}/fork", response_model=ChatRead, status_code=status.HTTP_201_CREATED)
def fork_chat(chat_id: int, request: ChatForkRequest, store: StoreDependency) -> ChatRead:
    """Branch a conversation into a new, independent chat.

    Every message up to and including the one forked from is copied verbatim
    into a new chat carrying the same repo/agent settings. Any agent-run turn
    among them gets its own cloned agent-core session -- same tool calls, same
    file snapshots -- so its trace and diff render identically from the new
    chat, and neither copy's undo can corrupt the other's (see
    ``AgentCoreService.clone_for_fork`` for why sharing the same repository is
    safe).
    """

    chat = _get_required_chat(store, chat_id)
    messages = store.list_chat_messages(chat_id)
    try:
        cut = next(
            index for index, message in enumerate(messages) if message.id == request.message_id
        )
    except StopIteration:
        raise HTTPException(status_code=404, detail="Message not found in this chat.") from None
    copied = messages[: cut + 1]
    copied_ids = {message.id for message in copied}

    from app.services.agent_core import store as agent_store

    sessions_by_anchor = {
        row["anchor_message_id"]: row
        for row in agent_store.sessions_for_chat(chat_id)
        if row.get("anchor_message_id")
    }
    still_running = [
        row
        for anchor_id, row in sessions_by_anchor.items()
        if anchor_id in copied_ids and row["status"] in _ACTIVE_AGENT_STATUSES
    ]
    if still_running:
        raise HTTPException(
            status_code=400,
            detail="This run hasn't finished yet, so this conversation can't be forked past it.",
        )

    new_chat = store.add(
        Chat(
            title=(f"{chat.title} (fork)")[:160],
            project_id=chat.project_id,
            repo_id=chat.repo_id,
            agent_mode=chat.agent_mode,
            agent_definition_id=chat.agent_definition_id,
            disabled_tools=list(chat.disabled_tools or []),
        )
    )
    id_map: dict[int, int] = {}
    for message in copied:
        clone = store.add(
            ChatMessage(
                chat_id=new_chat.id,
                role=message.role,
                content=message.content,
                prompt_tokens=message.prompt_tokens,
                completion_tokens=message.completion_tokens,
                total_tokens=message.total_tokens,
                duration_ms=message.duration_ms,
                thinking=message.thinking,
                response_kind=message.response_kind,
                provider_name=message.provider_name,
                model_name=message.model_name,
                route_name=message.route_name,
                finish_reason=message.finish_reason,
                trace_id=message.trace_id,
                metadata_json=message.metadata_json,
                # A generation row is durable worker state for the original
                # chat's own turn; the copy has no execution to resume, and the
                # unique index on generation_id would collide on a literal copy.
                generation_id=None,
                created_at=message.created_at,
            )
        )
        id_map[message.id] = clone.id
    store.db.commit()
    store.db.refresh(new_chat)

    # Best-effort: the agent-core session store is a separate SQLite database
    # from the chat ORM above, so this cannot share one transaction with it.
    # A clone that fails here still leaves a complete, readable chat behind --
    # that turn simply renders without a trace/diff, matching how AgentTurn
    # already handles an anchor with no session.
    from app.services.agent_core.service import AgentCoreService

    service = AgentCoreService()
    for old_anchor_id, session_row in sessions_by_anchor.items():
        new_anchor_id = id_map.get(old_anchor_id)
        if new_anchor_id is None:
            continue
        try:
            service.clone_for_fork(
                str(session_row["id"]), chat_id=new_chat.id, anchor_message_id=new_anchor_id
            )
        except Exception:
            _CHAT_LOG.exception("Failed to clone agent session %s for fork", session_row["id"])

    return ChatRead.model_validate(new_chat)


#: How many of a chat's most recent messages compaction always leaves untouched.
#: Matches both chat_history_turns' default and agent_core/context.py's
#: KEEP_RECENT without coupling to either -- this governs which persisted rows
#: survive a compact, not what one prompt sends to the model.
COMPACT_KEEP_RECENT = 8

_COMPACT_SUMMARY_SYSTEM_PROMPT = (
    "You compress an earlier portion of a conversation into a compact, faithful "
    "summary so the conversation can continue with less context. Preserve concrete "
    "facts, decisions, names, numbers, code identifiers, file paths, and any "
    "commitments made -- anything a later reply might need to stay consistent. Do "
    "not add commentary or claim actions were taken that were not. Write it as "
    "neutral third-person notes, not as a new reply in the conversation. Keep it "
    "under roughly 400 words."
)


@router.post("/chats/{chat_id}/compact", response_model=ChatCompactResponse)
def compact_chat(chat_id: int, request: ChatCompactRequest, store: StoreDependency) -> ChatCompactResponse:
    """Fold the older part of a conversation into one summary message.

    The most recent ``COMPACT_KEEP_RECENT`` messages, and any message that
    anchors an agent run, are left untouched -- deleting an anchor would
    permanently orphan that run's tool-call trace, diff and undo. Everything
    else older is summarized by the model and replaced with a single assistant
    message carrying ``response_kind="compaction_summary"``.
    """

    chat = _get_required_chat(store, chat_id)

    active_generation = store.db.scalar(
        select(ChatGeneration).where(
            ChatGeneration.chat_id == chat_id,
            ChatGeneration.status.in_(("queued", "running")),
        )
    )
    if active_generation is not None:
        raise HTTPException(status_code=409, detail="A response is still generating for this chat.")

    from app.services.agent_core import store as agent_store

    sessions = agent_store.sessions_for_chat(chat_id)
    if any(row["status"] in _ACTIVE_AGENT_STATUSES for row in sessions):
        raise HTTPException(status_code=409, detail="An agent run is still in progress for this chat.")

    messages = store.list_chat_messages(chat_id)

    def _no_op() -> ChatCompactResponse:
        return ChatCompactResponse(
            chat=ChatRead.model_validate(chat),
            summary_message=None,
            compacted_message_count=0,
            kept_message_count=len(messages),
        )

    if not messages:
        return _no_op()

    candidates = messages[:-COMPACT_KEEP_RECENT] if len(messages) > COMPACT_KEEP_RECENT else []
    to_keep = messages[-COMPACT_KEEP_RECENT:]
    if not candidates:
        return _no_op()

    anchor_ids = {row["anchor_message_id"] for row in sessions if row.get("anchor_message_id")}
    to_summarize = [message for message in candidates if message.id not in anchor_ids]
    preserved_ids = [message.id for message in candidates if message.id in anchor_ids]
    if not to_summarize:
        return _no_op()

    transcript = "\n\n".join(f"{message.role.upper()}: {message.content}" for message in to_summarize)
    transcript = transcript[-120_000:]
    try:
        client = get_llm_client(request.llm_id, num_predict=900, timeout=180, route_name="chat")
        result = client.chat_with_metadata(
            [
                LLMMessage(role="system", content=_COMPACT_SUMMARY_SYSTEM_PROMPT),
                LLMMessage(role="user", content=f"{transcript}\n\nSummarize the conversation above."),
            ],
            temperature=0.2,
            num_predict=900,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Could not generate a summary right now; nothing was changed."
        ) from exc

    summary = store.add(
        ChatMessage(
            chat_id=chat_id,
            role="assistant",
            content=result.content,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            duration_ms=result.duration_ms,
            response_kind="compaction_summary",
            provider_name=result.provider_name,
            model_name=result.model_name,
            route_name=result.route_name or "chat",
            finish_reason=result.finish_reason,
            metadata_json=json.dumps(
                {
                    "compacted_message_ids": [message.id for message in to_summarize],
                    "compacted_message_count": len(to_summarize),
                    "kept_message_ids": [message.id for message in to_keep],
                    "preserved_agent_anchor_ids": preserved_ids,
                },
                sort_keys=True,
            ),
            created_at=to_summarize[-1].created_at,
        )
    )
    # ChatGeneration.user_message_id/assistant_message_id are real FKs with
    # ondelete="SET NULL", and PRAGMA foreign_keys=ON is set on every connection
    # (app/db/session.py), so SQLite nulls those out on delete -- no cleanup
    # needed here. ChatEvent.message_id has no FK and is only ever read live
    # during an active generation (blocked above), so it going stale is harmless.
    store.db.execute(
        delete(ChatMessage).where(
            ChatMessage.chat_id == chat_id,
            ChatMessage.id.in_([message.id for message in to_summarize]),
        )
    )
    store.db.commit()
    store.db.refresh(summary)
    store.db.refresh(chat)

    return ChatCompactResponse(
        chat=ChatRead.model_validate(chat),
        summary_message=_chat_message_read(summary),
        compacted_message_count=len(to_summarize),
        kept_message_count=len(to_keep) + len(preserved_ids),
    )


@router.patch("/chats/{chat_id}", response_model=ChatRead)
def update_chat(chat_id: int, request: ChatUpdateRequest, store: StoreDependency) -> ChatRead:
    chat = _get_required_chat(store, chat_id)
    if request.title is not None:
        chat = store.rename_chat(chat_id, request.title)
    if request.pinned is not None:
        chat = store.set_chat_pinned(chat_id, request.pinned)
    # An empty string clears the workspace, which is a real choice ("stop
    # pointing this conversation at a folder") and distinct from not saying.
    if request.repo_id is not None:
        chat.repo_id = request.repo_id or None
    if request.agent_mode is not None:
        chat.agent_mode = request.agent_mode
    if request.agent_definition_id is not None:
        chat.agent_definition_id = request.agent_definition_id or None
    if request.disabled_tools is not None:
        chat.disabled_tools = request.disabled_tools
    store.db.commit()
    store.db.refresh(chat)
    return ChatRead.model_validate(chat)


@router.get("/chats/{chat_id}/tools", response_model=ChatToolsRead)
def list_chat_tools(chat_id: int, store: StoreDependency) -> ChatToolsRead:
    """Every tool this chat's agent could use, toggle-annotated.

    Returns the full candidate set regardless of the chat's current repo or
    permission mode -- a repo-only tool can be pre-toggled off before any
    repository is attached, and its ``requires_repo`` flag is what a caller
    uses to explain that instead of hiding the row.
    """

    chat = _get_required_chat(store, chat_id)
    from app.services.agent_core.tools.connectors import chat_tools_catalog
    from app.services.agent_core.tools.registry import ToolRegistry

    registry = ToolRegistry()
    catalog = chat_tools_catalog(registry, set(chat.disabled_tools or []))
    return ChatToolsRead(tools=catalog)


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
        # Collect the work first: building the runtime probes the configured model, so
        # a chat with no user messages must not pay for a loop body that never runs.
        user_messages = [
            message for message in store.list_chat_messages(chat_id) if message.role == "user"
        ]
        if user_messages:
            runtime = build_memory_runtime(profile)
            for message in user_messages:
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
