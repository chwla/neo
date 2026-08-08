from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import chat as chat_routes
from app.db.base import Base
from app.db.session import build_engine
from app.models import ChatGeneration, ChatMessage
from app.repositories.app_store import AppStore
from app.services.chat import NeoChatService
from app.services.llm import ProviderUsagePersistenceError


@pytest.fixture
def generation_session(tmp_path):
    engine = build_engine(f"sqlite:///{tmp_path / 'generation.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    db = factory()
    try:
        yield db, factory
    finally:
        db.close()
        engine.dispose()


def _request() -> SimpleNamespace:
    return SimpleNamespace()


@pytest.mark.parametrize(
    ("request_type", "field"),
    [
        (chat_routes.ChatSendRequest, "prompt"),
        (chat_routes.ChatMessageUpdateRequest, "content"),
    ],
)
def test_chat_request_rejects_whitespace_only_content(request_type, field) -> None:
    with pytest.raises(ValueError, match="must_not_be_blank"):
        request_type(**{field: " \t\n "})


def _profile() -> dict[str, object]:
    return {"id": "test-profile", "is_guest": False}


def _create_chat(store: AppStore) -> int:
    chat = store.create_chat()
    store.db.commit()
    return chat.id


def _create_generation(
    store: AppStore,
    chat_id: int,
    *,
    generation_id: str,
    status: str,
    heartbeat_at: datetime | None = None,
    worker_id: str | None = None,
    lease_token: str | None = None,
    partial_response: str = "",
) -> ChatGeneration:
    user = store.add_chat_message(chat_id, "user", "Explain durable generation recovery.")
    generation = ChatGeneration(
        id=generation_id,
        chat_id=chat_id,
        prompt=user.content,
        client_request_id=f"request-{generation_id}",
        user_message_id=user.id,
        status=status,
        status_detail=status.title(),
        partial_response=partial_response,
        worker_id=worker_id,
        lease_token=lease_token,
        heartbeat_at=heartbeat_at,
        attempt_count=0,
    )
    store.db.add(generation)
    store.db.commit()
    return generation


def test_completed_memory_extraction_is_shown_only_in_assistant_thinking(
    generation_session,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    assistant = store.add_chat_message(
        chat_id,
        "assistant",
        "Here is the answer to your question.",
        thinking="Provider reasoning.",
        metadata={"memory_extraction": {"status": "scheduled"}},
    )
    db.commit()

    NeoChatService._record_memory_extraction_result(
        db.get_bind(),
        assistant_message_id=assistant.id,
        result=SimpleNamespace(
            decisions=(
                SimpleNamespace(outcome="created"),
                SimpleNamespace(outcome="replaced"),
                SimpleNamespace(outcome="needs_review"),
            )
        ),
    )

    db.expire_all()
    persisted = db.get(ChatMessage, assistant.id)
    assert persisted is not None
    assert persisted.content == "Here is the answer to your question."
    assert persisted.thinking == (
        "Provider reasoning.\n\nSaved 2 durable memories after extraction and review."
    )
    assert json.loads(persisted.metadata_json or "{}") == {
        "memory_extraction": {"status": "completed", "saved_durable_memories": 2}
    }


def test_generation_start_persists_one_linked_user_before_queueing(
    generation_session,
    monkeypatch,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    spawned: list[str] = []
    monkeypatch.setattr(chat_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(chat_routes, "build_memory_runtime", lambda _profile_value: object())
    monkeypatch.setattr(chat_routes, "_detach_sources_for_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chat_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )
    payload = chat_routes.ChatSendRequest(
        prompt="Hello from one durable turn.",
        client_request_id="one-client-request",
    )

    first = chat_routes._start_chat_generation(_request(), store, chat_id, payload)
    second = chat_routes._start_chat_generation(_request(), store, chat_id, payload)

    users = list(
        db.scalars(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "user",
            )
        )
    )
    assert first.id == second.id
    assert len(users) == 1
    assert first.user_message_id == users[0].id
    assert users[0].content == payload.prompt
    assert first.status == "queued"
    assert first.worker_id is None
    assert first.lease_token is None
    assert db.scalar(select(func.count()).select_from(ChatGeneration)) == 1
    assert spawned == [first.id, first.id]


def test_polling_does_not_steal_a_non_stale_running_worker(
    generation_session,
    monkeypatch,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="fresh-generation",
        status="running",
        heartbeat_at=datetime.now(UTC),
        worker_id="another-live-worker",
        lease_token="another-live-lease",
        partial_response="still streaming",
    )
    spawned: list[str] = []
    monkeypatch.setattr(chat_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        chat_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )

    chat_routes._recover_generation(_request(), store, generation)
    db.refresh(generation)

    assert spawned == []
    assert generation.status == "running"
    assert generation.worker_id == "another-live-worker"
    assert generation.lease_token == "another-live-lease"
    assert generation.partial_response == "still streaming"


def test_stale_generation_is_atomically_reclaimed_without_erasing_partial_state(
    generation_session,
    monkeypatch,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    now = datetime.now(UTC)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="stale-generation",
        status="running",
        heartbeat_at=now - timedelta(seconds=chat_routes._lease_duration_seconds() + 1),
        worker_id="dead-worker",
        lease_token="dead-lease",
        partial_response="preserve this partial text",
    )
    spawned: list[str] = []
    monkeypatch.setattr(chat_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        chat_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )

    chat_routes._recover_generation(_request(), store, generation)
    claimed = chat_routes._claim_generation(
        db,
        generation.id,
        "replacement-lease",
        now=now,
    )
    competing_claim = chat_routes._claim_generation(
        db,
        generation.id,
        "competing-lease",
        now=now,
    )

    assert spawned == [generation.id]
    assert claimed is not None
    db.refresh(claimed)
    assert claimed.status == "running"
    assert claimed.worker_id == chat_routes.PROCESS_WORKER_ID
    assert claimed.lease_token == "replacement-lease"
    assert claimed.partial_response == "preserve this partial text"
    assert claimed.attempt_count == 1
    assert competing_claim is None


def test_queued_generation_survives_restart_with_its_persisted_user(
    generation_session,
    monkeypatch,
) -> None:
    db, factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    monkeypatch.setattr(chat_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(chat_routes, "_spawn_generation", lambda *_args: None)
    payload = chat_routes.ChatSendRequest(
        prompt="Continue after a process restart.",
        client_request_id="restart-boundary",
    )
    queued = chat_routes._start_chat_generation(_request(), store, chat_id, payload)
    queued_id = queued.id
    linked_user_id = queued.user_message_id
    db.close()

    captured: dict[str, object] = {}

    class RecoveredService:
        def __init__(self, worker_db) -> None:
            self.db = worker_db

        def stream_message(self, recovered_chat_id, prompt, **kwargs):
            captured.update(
                {
                    "chat_id": recovered_chat_id,
                    "prompt": prompt,
                    "user_message_id": kwargs["existing_user_message_id"],
                    "generation_id": kwargs["generation_id"],
                }
            )
            assistant = AppStore(self.db).upsert_generation_assistant(
                recovered_chat_id,
                kwargs["generation_id"],
                "Recovered exactly once.",
                response_kind="normal_chat",
            )
            self.db.commit()
            yield {"type": "chunk", "content": "Recovered exactly once."}
            yield {
                "type": "done",
                "message_id": assistant.id,
                "reply": "Recovered exactly once.",
                "response_kind": "normal_chat",
                "finish_reason": "stop",
            }

    monkeypatch.setattr(chat_routes, "profile_database", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: factory())
    monkeypatch.setattr(
        chat_routes,
        "_generation_service",
        lambda worker_db, _chat, _llm_id, **_options: RecoveredService(worker_db),
    )

    chat_routes._run_chat_generation(_profile(), queued_id)

    verify = factory()
    try:
        generation = verify.get(ChatGeneration, queued_id)
        messages = list(
            verify.scalars(
                select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)
            )
        )
        assert generation is not None
        assert generation.status == "completed"
        assert generation.user_message_id == linked_user_id
        assert [message.role for message in messages] == ["user", "assistant"]
        assert captured == {
            "chat_id": chat_id,
            "prompt": payload.prompt,
            "user_message_id": linked_user_id,
            "generation_id": queued_id,
        }
    finally:
        verify.close()


def test_generation_records_usage_persistence_failure_with_neo_attribution(
    generation_session,
    monkeypatch,
) -> None:
    db, factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="usage-persistence-failure",
        status="queued",
    )
    generation_id = generation.id
    db.close()

    class FailingService:
        def stream_message(self, *_args, **_kwargs):
            raise ProviderUsagePersistenceError("database is locked")
            yield  # pragma: no cover - keeps this method a generator

    monkeypatch.setattr(chat_routes, "profile_database", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(chat_routes, "SessionLocal", lambda: factory())
    monkeypatch.setattr(
        chat_routes,
        "_generation_service",
        lambda *_args, **_kwargs: FailingService(),
    )

    chat_routes._run_chat_generation(_profile(), generation_id)

    verify = factory()
    try:
        persisted = verify.get(ChatGeneration, generation_id)
        assert persisted is not None
        assert persisted.status == "failed"
        assert persisted.status_detail == "Neo persistence failed"
        assert persisted.error == (
            "Neo could not persist provider usage data. Details: database is locked"
        )
    finally:
        verify.close()


def test_recovery_updates_the_generation_assistant_instead_of_duplicating_it(
    generation_session,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    now = datetime.now(UTC)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="assistant-crash-window",
        status="running",
        heartbeat_at=now - timedelta(seconds=chat_routes._lease_duration_seconds() + 1),
        worker_id="dead-worker",
        lease_token="dead-lease",
    )
    first = store.upsert_generation_assistant(
        chat_id,
        generation.id,
        "Response saved before the worker crashed.",
        response_kind="normal_chat",
    )
    db.commit()
    claimed = chat_routes._claim_generation(
        db,
        generation.id,
        "recovery-lease",
        now=now,
    )
    assert claimed is not None
    service = object.__new__(NeoChatService)
    service.db = db
    service.store = store

    second = service._persist_stream_assistant(
        chat_id,
        "Final recovered response.",
        generation_id=generation.id,
        generation_lease_token="recovery-lease",
        response_kind="normal_chat",
        finish_reason="stop",
    )
    db.commit()

    assistants = list(
        db.scalars(
            select(ChatMessage).where(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "assistant",
            )
        )
    )
    assert first.id == second.id
    assert len(assistants) == 1
    assert assistants[0].content == "Final recovered response."
    assert assistants[0].generation_id == generation.id


def test_rerun_supersedes_affected_worker_and_queues_from_the_edited_user(
    generation_session,
    monkeypatch,
) -> None:
    db, _factory = generation_session
    store = AppStore(db)
    chat_id = _create_chat(store)
    edited_user = store.add_chat_message(chat_id, "user", "Original question")
    store.add_chat_message(chat_id, "assistant", "Original answer")
    later_user = store.add_chat_message(chat_id, "user", "Later question")
    store.add_chat_message(chat_id, "assistant", "Later answer")
    old_generation = ChatGeneration(
        id="generation-being-superseded",
        chat_id=chat_id,
        prompt=later_user.content,
        user_message_id=later_user.id,
        status="running",
        status_detail="Running",
        partial_response="Old partial",
        worker_id="old-worker",
        lease_token="old-lease",
        heartbeat_at=datetime.now(UTC),
        attempt_count=1,
    )
    db.add(old_generation)
    db.commit()
    spawned: list[str] = []
    monkeypatch.setattr(chat_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(chat_routes, "build_memory_runtime", lambda _profile_value: object())
    monkeypatch.setattr(chat_routes, "_detach_sources_for_message", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        chat_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )
    payload = chat_routes.ChatSendRequest(
        prompt="Edited question",
        client_request_id="edited-rerun-request",
    )

    response = chat_routes.rerun_edited_chat_message(
        chat_id,
        edited_user.id,
        payload,
        _request(),
        store,
    )

    db.refresh(old_generation)
    db.refresh(edited_user)
    remaining_messages = list(
        db.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id)
        )
    )
    new_generation = db.get(ChatGeneration, response.generation.id)
    assert old_generation.status == "failed"
    assert old_generation.status_detail == "Superseded"
    assert edited_user.content == "Edited question"
    assert remaining_messages == [edited_user]
    assert new_generation is not None
    assert new_generation.user_message_id == edited_user.id
    assert new_generation.status == "queued"
    assert spawned == [new_generation.id]
