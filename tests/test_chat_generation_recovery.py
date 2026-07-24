from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes import memory as memory_routes
from app.db.base import Base
from app.db.session import build_engine
from app.models import ChatGeneration, ChatMessage
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService


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


def _profile() -> dict[str, object]:
    return {"id": "test-profile", "is_guest": False}


def _create_chat(store: MemoryStore) -> int:
    chat = store.create_chat()
    store.db.commit()
    return chat.id


def _create_generation(
    store: MemoryStore,
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


def test_generation_start_persists_one_linked_user_before_queueing(
    generation_session,
    monkeypatch,
) -> None:
    db, _factory = generation_session
    store = MemoryStore(db)
    chat_id = _create_chat(store)
    spawned: list[str] = []
    monkeypatch.setattr(memory_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        memory_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )
    payload = memory_routes.ChatSendRequest(
        prompt="Hello from one durable turn.",
        client_request_id="one-client-request",
    )

    first = memory_routes._start_chat_generation(_request(), store, chat_id, payload)
    second = memory_routes._start_chat_generation(_request(), store, chat_id, payload)

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
    store = MemoryStore(db)
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
    monkeypatch.setattr(memory_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        memory_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )

    memory_routes._recover_generation(_request(), store, generation)
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
    store = MemoryStore(db)
    chat_id = _create_chat(store)
    now = datetime.now(UTC)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="stale-generation",
        status="running",
        heartbeat_at=now - timedelta(seconds=memory_routes._lease_duration_seconds() + 1),
        worker_id="dead-worker",
        lease_token="dead-lease",
        partial_response="preserve this partial text",
    )
    spawned: list[str] = []
    monkeypatch.setattr(memory_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        memory_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )

    memory_routes._recover_generation(_request(), store, generation)
    claimed = memory_routes._claim_generation(
        db,
        generation.id,
        "replacement-lease",
        now=now,
    )
    competing_claim = memory_routes._claim_generation(
        db,
        generation.id,
        "competing-lease",
        now=now,
    )

    assert spawned == [generation.id]
    assert claimed is not None
    db.refresh(claimed)
    assert claimed.status == "running"
    assert claimed.worker_id == memory_routes.PROCESS_WORKER_ID
    assert claimed.lease_token == "replacement-lease"
    assert claimed.partial_response == "preserve this partial text"
    assert claimed.attempt_count == 1
    assert competing_claim is None


def test_queued_generation_survives_restart_with_its_persisted_user(
    generation_session,
    monkeypatch,
) -> None:
    db, factory = generation_session
    store = MemoryStore(db)
    chat_id = _create_chat(store)
    monkeypatch.setattr(memory_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(memory_routes, "_spawn_generation", lambda *_args: None)
    payload = memory_routes.ChatSendRequest(
        prompt="Continue after a process restart.",
        client_request_id="restart-boundary",
    )
    queued = memory_routes._start_chat_generation(_request(), store, chat_id, payload)
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
            assistant = MemoryStore(self.db).upsert_generation_assistant(
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

    monkeypatch.setattr(memory_routes, "profile_database", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(memory_routes, "SessionLocal", lambda: factory())
    monkeypatch.setattr(
        memory_routes,
        "_generation_service",
        lambda worker_db, _chat, _llm_id: RecoveredService(worker_db),
    )

    memory_routes._run_chat_generation(_profile(), queued_id)

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


def test_recovery_updates_the_generation_assistant_instead_of_duplicating_it(
    generation_session,
) -> None:
    db, _factory = generation_session
    store = MemoryStore(db)
    chat_id = _create_chat(store)
    now = datetime.now(UTC)
    generation = _create_generation(
        store,
        chat_id,
        generation_id="assistant-crash-window",
        status="running",
        heartbeat_at=now - timedelta(seconds=memory_routes._lease_duration_seconds() + 1),
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
    claimed = memory_routes._claim_generation(
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
    store = MemoryStore(db)
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
    monkeypatch.setattr(memory_routes, "session_for", lambda _request: _profile())
    monkeypatch.setattr(
        memory_routes,
        "_spawn_generation",
        lambda _profile_value, generation_id: spawned.append(generation_id),
    )
    payload = memory_routes.ChatSendRequest(
        prompt="Edited question",
        client_request_id="edited-rerun-request",
    )

    response = memory_routes.rerun_edited_chat_message(
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
