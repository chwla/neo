from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Memory, ProfileFact
from app.models.enums import CandidateStatus, CandidateType, MemoryType
from app.repositories.memory_store import MemoryStore
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import (
    ExtractedItem,
    ExtractionRequest,
    ExtractionResult,
    MemoryExtractionService,
)
from app.services.review import MemoryReviewRequest, MemoryReviewService


@pytest.fixture()
def store() -> MemoryStore:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    try:
        yield MemoryStore(session)
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize("prompt", ["iam soham", "I am Soham", "I'm Soham"])
def test_name_introductions_create_a_durable_name_identity(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert [(item.attributes["key"], item.attributes["value"]) for item in result.identity] == [
        ("name", "Soham")
    ]


@pytest.mark.parametrize(
    "prompt",
    ["I am bored", "I'm tired", "I am happy", "I am feeling sick"],
)
def test_transient_states_are_never_extracted_as_profile_facts(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert result.identity == []


def test_memory_pipeline_persists_name_and_answers_who_am_i(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    extraction = extractor.extract(ExtractionRequest(text="iam soham"))
    candidates = extractor.persist_and_accept(store, extraction)
    store.db.commit()

    assert [candidate.status for candidate in candidates] == [CandidateStatus.ACCEPTED]
    assert [(fact.key, fact.value) for fact in store.list_profile()] == [("name", "Soham")]
    assert DirectMemoryAnswerService().answer(store, "who am i") == "From memory, your name is Soham."


def test_invalid_identity_from_another_entry_point_is_rejected_before_persistence(
    store: MemoryStore,
) -> None:
    extraction = ExtractionResult(
        identity=[
            ExtractedItem(
                candidate_type=CandidateType.IDENTITY,
                text="occupation = bored",
                confidence=0.99,
                importance=8,
                attributes={"key": "occupation", "value": "bored"},
                reasoning="Simulated external extractor output.",
            )
        ]
    )
    candidate = MemoryExtractionService().persist_candidates(store, extraction)[0]

    result = MemoryReviewService().review(
        store,
        MemoryReviewRequest(candidate_id=candidate.id, decision=CandidateStatus.ACCEPTED),
    )

    assert result.status == CandidateStatus.REJECTED
    assert store.list_profile() == []


def test_legacy_transient_profile_fact_is_retired_before_profile_answer(store: MemoryStore) -> None:
    store.add(ProfileFact(key="occupation", value="bored", confidence=0.82))
    store.add(ProfileFact(key="name", value="Soham", confidence=0.82))
    store.add(Memory(memory_type=MemoryType.IDENTITY, memory_text="occupation = bored"))
    store.db.commit()

    answer = DirectMemoryAnswerService().answer(store, "who am i")

    assert answer == "From memory, your name is Soham."
    assert [(fact.key, fact.value) for fact in store.list_profile()] == [("name", "Soham")]
    memory = next(memory for memory in store.list_memories(active_only=False) if memory.memory_text == "occupation = bored")
    assert memory.is_active is False
