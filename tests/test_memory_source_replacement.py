from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text as sql_text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.api.routes.memory as memory_routes
import app.models  # noqa: F401
from app.api.deps import get_store
from app.api.routes.memory import router
from app.db.base import Base
from app.models import ChatGeneration
from app.models.enums import CandidateStatus, MemoryType
from app.repositories.memory_store import MemoryStore
from app.services.extraction import ExtractionRequest, MemoryExtractionService


@pytest.fixture()
def store() -> Iterator[MemoryStore]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield MemoryStore(session)
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def api_client(store: MemoryStore) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(router, prefix="/api")

    def override_store() -> Iterator[MemoryStore]:
        yield store

    app.dependency_overrides[get_store] = override_store
    with TestClient(app) as client:
        yield client


def _persist_message(
    store: MemoryStore,
    text: str,
    *,
    chat_id: int | None = None,
):
    chat = store.get_chat(chat_id) if chat_id is not None else store.create_chat()
    assert chat is not None
    message = store.add_chat_message(chat.id, "user", text)
    extractor = MemoryExtractionService()
    candidates = extractor.persist_and_accept(
        store,
        extractor.extract(
            ExtractionRequest(
                text=text,
                source_conversation_id=chat.id,
                source_message_id=message.id,
                source_timestamp=message.created_at,
            ),
        ),
    )
    store.db.commit()
    return chat, message, candidates


def test_editing_a_message_to_the_same_statement_reuses_its_memory(
    store: MemoryStore,
    api_client: TestClient,
) -> None:
    text = "my occupation is software engineer"
    chat, message, _ = _persist_message(store, text)
    original = store.active_memories_by_type(MemoryType.IDENTITY)[0]

    response = api_client.patch(
        f"/api/chats/{chat.id}/messages/{message.id}",
        json={"content": text},
    )

    assert response.status_code == 200
    active = store.active_memories_by_type(MemoryType.IDENTITY)
    assert [memory.id for memory in active] == [original.id]
    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("occupation", "software engineer"),
    ]
    sources = store.list_memory_sources(original.id, active_only=False)
    assert [(source.is_active, source.detachment_reason) for source in sources] == [
        (True, None),
    ]
    assert store.list_candidates(CandidateStatus.REJECTED) == []


def test_editing_a_message_to_a_correction_archives_only_the_old_fact(
    store: MemoryStore,
    api_client: TestClient,
) -> None:
    chat, message, _ = _persist_message(store, "my occupation is software engineer")
    original = store.active_memories_by_type(MemoryType.IDENTITY)[0]

    response = api_client.patch(
        f"/api/chats/{chat.id}/messages/{message.id}",
        json={"content": "my occupation is product designer"},
    )

    assert response.status_code == 200
    assert original.status == "archived"
    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("occupation", "product designer"),
    ]
    assert [
        memory.memory_text for memory in store.active_memories_by_type(MemoryType.IDENTITY)
    ] == ["occupation = product designer"]


def test_rerunning_the_same_statement_can_reextract_the_replacement_source(
    store: MemoryStore,
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "my occupation is software engineer"
    chat, message, _ = _persist_message(store, text)
    original = store.active_memories_by_type(MemoryType.IDENTITY)[0]

    def fake_start_generation(_request, route_store, chat_id, payload, *, user_message_id=None):
        extractor = MemoryExtractionService()
        extractor.persist_and_accept(
            route_store,
            extractor.extract(
                ExtractionRequest(
                    text=payload.prompt,
                    source_conversation_id=chat_id,
                    source_message_id=user_message_id,
                ),
            ),
        )
        generation = ChatGeneration(
            id=str(uuid.uuid4()),
            chat_id=chat_id,
            prompt=payload.prompt,
            user_message_id=user_message_id,
            status="completed",
            status_detail="Completed",
            partial_response="",
            reply="",
        )
        route_store.db.add(generation)
        route_store.db.commit()
        route_store.db.refresh(generation)
        return generation

    monkeypatch.setattr(memory_routes, "_start_chat_generation", fake_start_generation)
    response = api_client.post(
        f"/api/chats/{chat.id}/messages/{message.id}/rerun",
        json={"prompt": text},
    )

    assert response.status_code == 200
    assert [memory.id for memory in store.active_memories_by_type(MemoryType.IDENTITY)] == [
        original.id,
    ]
    assert store.list_memory_sources(original.id)[0].source_message_id == message.id
    assert store.list_candidates(CandidateStatus.REJECTED) == []


def test_replacing_one_of_multiple_sources_preserves_the_other_support(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    chat = store.create_chat()
    source_ids: list[int] = []
    for _ in range(2):
        message = store.add_chat_message(chat.id, "user", "i love playing chess")
        source_ids.append(message.id)
        extractor.persist_and_accept(
            store,
            extractor.extract(
                ExtractionRequest(
                    text=message.content,
                    source_conversation_id=chat.id,
                    source_message_id=message.id,
                ),
            ),
        )
    store.db.commit()
    chess = store.active_memories_by_type(MemoryType.PREFERENCE)[0]

    store.detach_memory_sources_for_message(source_ids[0], reason="replacement")
    extractor.persist_and_accept(
        store,
        extractor.extract(
            ExtractionRequest(
                text="i find samurais to be interesting",
                source_conversation_id=chat.id,
                source_message_id=source_ids[0],
            ),
        ),
    )

    assert chess.status == "active"
    assert [source.source_message_id for source in store.list_memory_sources(chess.id)] == [
        source_ids[1],
    ]
    assert {(item.category, item.value) for item in store.list_preferences()} == {
        ("interest", "playing chess"),
        ("interest", "samurais"),
    }


def test_deleting_a_chat_creates_a_tombstone_that_a_new_source_cannot_bypass(
    store: MemoryStore,
    api_client: TestClient,
) -> None:
    text = "my occupation is software engineer"
    chat, _message, _ = _persist_message(store, text)
    memory = store.active_memories_by_type(MemoryType.IDENTITY)[0]

    response = api_client.delete(f"/api/chats/{chat.id}")

    assert response.status_code == 204
    assert memory.status == "deleted"
    assert store.list_profile() == []

    _new_chat, _new_message, candidates = _persist_message(store, text)
    assert [candidate.status for candidate in candidates] == [CandidateStatus.REJECTED]
    assert store.active_memories_by_type(MemoryType.IDENTITY) == []
    assert store.list_profile() == []


def test_explicit_memory_deletion_cannot_be_mistaken_for_source_replacement(
    store: MemoryStore,
) -> None:
    text = "my occupation is software engineer"
    chat, message, _ = _persist_message(store, text)
    memory = store.active_memories_by_type(MemoryType.IDENTITY)[0]
    store.delete_memory(memory.id)
    store.db.commit()

    # Even a later edit lifecycle on the original message may not downgrade an
    # explicit deleted tombstone into a source-scoped replacement.
    store.detach_memory_sources_for_message(message.id, reason="replacement")
    extractor = MemoryExtractionService()
    candidates = extractor.persist_and_accept(
        store,
        extractor.extract(
            ExtractionRequest(
                text=text,
                source_conversation_id=chat.id,
                source_message_id=message.id,
            ),
        ),
    )

    assert memory.status == "deleted"
    assert [candidate.status for candidate in candidates] == [CandidateStatus.REJECTED]
    assert store.active_memories_by_type(MemoryType.IDENTITY) == []
    assert store.list_profile() == []


def test_detachment_reason_migration_upgrades_an_existing_source_table(tmp_path) -> None:
    from app.db.session import initialize_database

    database_url = f"sqlite+pysqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            sql_text(
                """
                CREATE TABLE memory_sources (
                    id INTEGER PRIMARY KEY,
                    memory_id INTEGER NOT NULL,
                    source_conversation_id INTEGER,
                    source_message_id INTEGER,
                    source_sentence TEXT NOT NULL,
                    source_fingerprint VARCHAR(64) NOT NULL,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )
    engine.dispose()

    initialize_database(database_url)
    migrated_engine = create_engine(database_url, future=True)
    with migrated_engine.connect() as connection:
        columns = {
            row[1] for row in connection.execute(sql_text("PRAGMA table_info(memory_sources)"))
        }
    migrated_engine.dispose()

    assert "detachment_reason" in columns
