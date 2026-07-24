from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.models import Memory, ProfileFact
from app.models.enums import CandidateStatus, CandidateType, MemoryType, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService
from app.services.conflicts import ConflictResolutionService
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import (
    ExtractedItem,
    ExtractionRequest,
    ExtractionResult,
    MemoryExtractionService,
)
from app.services.lifecycle import MemoryLifecycleService
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


@pytest.mark.parametrize(
    ("prompt", "key", "value"),
    [
        ("Call me Jordan Rivera", "name", "Jordan Rivera"),
        ("I go by Riya", "name", "Riya"),
        ("I turned 22", "age", "22"),
        ("I currently live in Bengaluru", "location", "Bengaluru"),
        ("I'm based in Mumbai", "location", "Mumbai"),
        ("I am from New Delhi", "location", "New Delhi"),
        ("I moved to Pune", "location", "Pune"),
        ("My job is software engineer", "occupation", "software engineer"),
        ("I'm currently a product designer", "occupation", "product designer"),
        ("My country is India", "country", "India"),
        ("My nationality is Indian", "nationality", "Indian"),
        ("I attend Delhi University", "education", "Delhi University"),
    ],
)
def test_explicit_profile_phrasings_use_typed_identity_slots(
    prompt: str,
    key: str,
    value: str,
) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert [(item.attributes["key"], item.attributes["value"]) for item in result.identity] == [
        (key, value)
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "I am ready",
        "I am lost",
        "I am really busy",
        "I am planning a trip",
        "I am available",
        "I am back",
    ],
)
def test_additional_transient_or_action_states_never_become_names(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert result.identity == []


@pytest.mark.parametrize(
    "prompt",
    [
        "I need to know the latest iPhone price",
        "I want to search today's weather",
        "I like this answer",
        "I love this response",
    ],
)
def test_one_off_requests_and_feedback_are_not_saved_as_durable_memory(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert result.items == []


def test_active_work_statement_is_categorized_as_projects_not_identity() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(text="i am currently building neo and playboxd")
    )

    assert result.identity == []
    assert [
        (item.attributes["name"], item.attributes["description"]) for item in result.projects
    ] == [
        ("Neo", "Currently building Neo"),
        ("Playboxd", "Currently building Playboxd"),
    ]


@pytest.mark.parametrize(
    "prompt",
    [
        "My projects are Neo, Playboxd, and Atlas",
        "I am currently building Neo, Playboxd, and Atlas",
    ],
)
def test_project_lists_create_independent_project_slots(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert result.identity == []
    assert [item.attributes["name"] for item in result.projects] == [
        "Neo",
        "Playboxd",
        "Atlas",
    ]


def test_named_multiword_project_is_extracted_without_touching_identity() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(text="I am building a project called Project Atlas")
    )

    assert result.identity == []
    assert [item.attributes["name"] for item in result.projects] == ["Project Atlas"]


def test_semicolon_separated_profile_and_projects_are_all_typed_correctly() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(
            text=(
                "Call me Priya Nair; I'm based in Chennai; "
                "I'm currently a product designer; "
                "My projects are Orion, Vega, and Nova."
            )
        )
    )

    assert [(item.attributes["key"], item.attributes["value"]) for item in result.identity] == [
        ("name", "Priya Nair"),
        ("location", "Chennai"),
        ("occupation", "product designer"),
    ]
    assert [item.attributes["name"] for item in result.projects] == [
        "Orion",
        "Vega",
        "Nova",
    ]


def test_declarative_project_list_does_not_trigger_a_direct_recall_answer(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    extractor.persist_and_accept(
        store,
        extractor.extract(ExtractionRequest(text="My projects are Orion and Vega")),
    )

    assert DirectMemoryAnswerService().answer(store, "My projects are Orion and Vega") is None
    assert "Orion" in (DirectMemoryAnswerService().answer(store, "Show my active projects") or "")


def test_explicit_corrections_supersede_only_their_own_profile_slots(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    for prompt in (
        "Call me Priya Nair; I'm based in Chennai; I'm currently a product designer",
        "Actually, call me Asha Nair; I moved to Bengaluru; I turned 29",
    ):
        extractor.persist_and_accept(
            store,
            extractor.extract(ExtractionRequest(text=prompt)),
        )
    store.db.commit()

    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("age", "29"),
        ("location", "Bengaluru"),
        ("name", "Asha Nair"),
        ("occupation", "product designer"),
    ]
    direct = DirectMemoryAnswerService()
    assert direct.answer(store, "What is my name?") == "Your name is Asha Nair."
    assert direct.answer(store, "Where do I live?") == "You are in Bengaluru."


def test_profile_and_projects_remain_in_independent_slots(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    for prompt in (
        "my name is soham chawla",
        "i am 21 years old",
        "i am currently building neo and playboxd",
    ):
        extractor.persist_and_accept(store, extractor.extract(ExtractionRequest(text=prompt)))
    store.db.commit()

    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("age", "21"),
        ("name", "Soham Chawla"),
    ]
    assert sorted(project.name for project in store.list_projects(ProjectStatus.ACTIVE)) == [
        "Neo",
        "Playboxd",
    ]
    assert DirectMemoryAnswerService().answer(store, "what is my name") == (
        "Your name is Soham Chawla."
    )


def test_memory_pipeline_persists_name_and_answers_who_am_i(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    extraction = extractor.extract(ExtractionRequest(text="iam soham"))
    candidates = extractor.persist_and_accept(store, extraction)
    store.db.commit()

    assert [candidate.status for candidate in candidates] == [CandidateStatus.ACCEPTED]
    assert [(fact.key, fact.value) for fact in store.list_profile()] == [("name", "Soham")]
    assert (
        DirectMemoryAnswerService().answer(store, "who am i") == "From memory, your name is Soham."
    )


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
    memory = next(
        memory
        for memory in store.list_memories(active_only=False)
        if memory.memory_text == "occupation = bored"
    )
    assert memory.is_active is False


def test_invalid_name_restores_the_valid_identity_it_superseded(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    extractor.persist_and_accept(
        store,
        extractor.extract(ExtractionRequest(text="my name is soham chawla")),
    )
    correct_fact = store.active_profile_by_key("name")[0]
    correct_memory = next(
        memory
        for memory in store.active_memories_by_type(MemoryType.IDENTITY)
        if memory.memory_text == "name = Soham Chawla"
    )

    invalid_fact = store.add(
        ProfileFact(key="name", value="Currently Building Neo", confidence=0.82)
    )
    ConflictResolutionService().supersede_profile_key(store, invalid_fact)
    invalid_memory = store.add(
        Memory(
            memory_type=MemoryType.IDENTITY,
            memory_text="name = Currently Building Neo",
            source_sentence="i am currently building neo and playboxd",
            canonical_slot="identity:name",
            supersedes_id=correct_memory.id,
        )
    )
    MemoryLifecycleService().supersede(
        store,
        correct_memory,
        invalid_memory,
        "Simulate a legacy misclassification.",
    )
    store.db.commit()

    answer = DirectMemoryAnswerService().answer(store, "what is my name")

    assert answer == "Your name is Soham Chawla."
    assert correct_fact.is_active is True
    assert invalid_fact.is_active is False
    assert correct_memory.is_active is True
    assert correct_memory.status == "active"
    assert invalid_memory.is_active is False
    assert invalid_memory.status == "deleted"


def test_chat_repair_reclassifies_legacy_identity_source_as_projects(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    extractor.persist_and_accept(
        store,
        extractor.extract(ExtractionRequest(text="my name is soham chawla")),
    )
    correct_memory = next(
        memory
        for memory in store.active_memories_by_type(MemoryType.IDENTITY)
        if memory.memory_text == "name = Soham Chawla"
    )
    invalid_fact = store.add(
        ProfileFact(key="name", value="Currently Building Neo", confidence=0.82)
    )
    ConflictResolutionService().supersede_profile_key(store, invalid_fact)
    invalid_memory = store.add(
        Memory(
            memory_type=MemoryType.IDENTITY,
            memory_text="name = Currently Building Neo",
            source_sentence="i am currently building neo and playboxd",
            canonical_slot="identity:name",
            supersedes_id=correct_memory.id,
        )
    )
    MemoryLifecycleService().supersede(
        store,
        correct_memory,
        invalid_memory,
        "Simulate a legacy misclassification.",
    )
    store.db.commit()

    service = object.__new__(NeoChatService)
    service.store = store
    service.db = store.db
    service.extractor = extractor
    service.extract_user_prompt("what is my name", chat_id=42)

    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("name", "Soham Chawla"),
    ]
    assert sorted(project.name for project in store.list_projects(ProjectStatus.ACTIVE)) == [
        "Neo",
        "Playboxd",
    ]
