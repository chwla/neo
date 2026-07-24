from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import initialize_database
from app.models.enums import CandidateStatus, GoalStatus, MemoryType
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import (
    ConversationMessage,
    ExtractionRequest,
    MemoryExtractionService,
)


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


def test_graduation_statement_extracts_typed_education_and_one_event() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(
            text=(
                "i recently graduated from bits pilani with "
                "Bachelors of engineering in computer science"
            ),
        ),
    )

    assert len(result.education) == 1
    assert result.education[0].attributes == {
        "institution": "BITS Pilani",
        "degree": "Bachelor of Engineering",
        "field_of_study": "Computer Science",
        "graduated": 1,
        "graduation_date": None,
        "source_sentence": (
            "i recently graduated from bits pilani with "
            "Bachelors of engineering in computer science"
        ),
        "canonical_slot": "education:bits_pilani",
    }
    assert [(item.text, item.attributes["event_date"]) for item in result.events] == [
        ("Graduated from BITS Pilani", None),
    ]


@pytest.mark.parametrize(
    ("prompt", "candidate_type", "value"),
    [
        ("i lovee playing chess", "preference", "playing chess"),
        ("i find samurais to be interesting", "preference", "samurais"),
        (
            "bobby fischer was my fabourite chess player",
            "preference",
            "Bobby Fischer",
        ),
        ("i want to master programming", "goal", "master programming"),
        (
            "i prioritise working with python and c++/c",
            "preference",
            "Python, C++, C",
        ),
        (
            "i am currently playing ghost of yotei",
            "activity",
            "playing Ghost of Yotei",
        ),
    ],
)
def test_transcript_typos_and_declarations_are_typed(
    prompt: str,
    candidate_type: str,
    value: str,
) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert len(result.items) == 1
    assert result.items[0].candidate_type.value == candidate_type
    assert value in {
        str(result.items[0].attributes.get("value") or ""),
        str(result.items[0].attributes.get("goal") or ""),
        str(result.items[0].attributes.get("activity") or ""),
    }


def test_location_statement_stores_location_and_country() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(text="i live in new delhi, india"),
    )

    assert [(item.attributes["key"], item.attributes["value"]) for item in result.identity] == [
        ("location", "New Delhi"),
        ("country", "India"),
    ]


def test_six_month_goal_is_anchored_to_source_timestamp() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(
            text="i want to get into faang in 6 months",
            source_timestamp=datetime(2026, 7, 23, 14, 30, tzinfo=UTC),
        ),
    )

    assert result.goals[0].attributes["goal"] == "get into faang"
    assert result.goals[0].attributes["horizon_months"] == 6
    assert result.goals[0].attributes["target_date"] == "2027-01-23"


@pytest.mark.parametrize(
    "prompt",
    [
        "I want to search today's weather",
        "I want to know the latest iPhone model",
        "What do I want to build?",
        "Explain what it means to love playing chess.",
        '"I am a software engineer"',
        "She said I am a software engineer.",
    ],
)
def test_queries_requests_and_quoted_claims_do_not_become_memory(prompt: str) -> None:
    result = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert result.items == []


def test_repeated_graduation_has_one_typed_record_and_independent_sources(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    prompt = (
        "i recently graduated from bits pilani with Bachelors of engineering in computer science"
    )
    for message_id in (101, 102):
        request = ExtractionRequest(
            text=prompt,
            source_conversation_id=7,
            source_message_id=message_id,
        )
        extraction = extractor.extract(request)
        candidates = extractor.persist_and_accept(store, extraction)
        assert (
            extractor.format_persisted_acknowledgement(
                request,
                extraction,
                candidates,
            )
            == "Got it — I’ve saved that to your memory."
        )
    store.db.commit()

    assert len(store.list_education()) == 1
    assert len(store.list_events()) == 1
    education_memory = store.active_memories_by_type(MemoryType.EDUCATION)[0]
    event_memory = store.active_memories_by_type(MemoryType.LIFE_FACT)[0]
    assert len(store.list_memory_sources(education_memory.id)) == 2
    assert len(store.list_memory_sources(event_memory.id)) == 2

    store.detach_memory_sources_for_message(101)
    assert len(store.list_education()) == 1
    assert len(store.list_events()) == 1

    store.detach_memory_sources_for_message(102)
    assert store.list_education() == []
    assert store.list_events() == []
    assert education_memory.status == "deleted"
    assert event_memory.status == "deleted"


def test_full_transcript_persists_all_expected_typed_records(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    source_timestamp = datetime(2026, 7, 23, tzinfo=UTC)
    prompts = [
        ("i recently graduated from bits pilani with Bachelors of engineering in computer science"),
        "i am a software engineer",
        "i lovee playing chess",
        "i find samurais to be interesting",
        "bobby fischer was my fabourite chess player",
        "i live in new delhi, india",
        "i want to master programming",
        "i want to get into faang in 6 months",
        "i prioritise working with python and c++/c",
        "i am currently playing ghost of yotei",
    ]
    for message_id, prompt in enumerate(prompts, start=1):
        extractor.persist_and_accept(
            store,
            extractor.extract(
                ExtractionRequest(
                    text=prompt,
                    source_conversation_id=1,
                    source_message_id=message_id,
                    source_timestamp=source_timestamp,
                ),
            ),
        )
    store.db.commit()

    assert [(fact.key, fact.value) for fact in store.list_profile()] == [
        ("country", "India"),
        ("location", "New Delhi"),
        ("occupation", "software engineer"),
    ]
    assert [
        (record.institution, record.degree, record.field_of_study)
        for record in store.list_education()
    ] == [("BITS Pilani", "Bachelor of Engineering", "Computer Science")]
    assert {(item.category, item.value) for item in store.list_preferences()} == {
        ("interest", "playing chess"),
        ("interest", "samurais"),
        ("favorite_chess_player", "Bobby Fischer"),
        ("programming_language_priority", "Python, C++, C"),
    }
    assert {
        (goal.goal, goal.horizon_months, goal.target_date)
        for goal in store.list_goals(GoalStatus.ACTIVE)
    } == {
        ("master programming", None, None),
        ("get into faang", 6, datetime(2027, 1, 23).date()),
    }
    assert [event.event for event in store.list_events()] == ["Graduated from BITS Pilani"]
    assert [activity.activity for activity in store.list_activities(now=source_timestamp)] == [
        "playing Ghost of Yotei"
    ]


def test_direct_recall_covers_new_typed_memory(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    prompts = [
        ("i recently graduated from bits pilani with Bachelors of engineering in computer science"),
        "i lovee playing chess",
        "i find samurais to be interesting",
        "bobby fischer was my fabourite chess player",
        "i prioritise working with python and c++/c",
        "i am currently playing ghost of yotei",
    ]
    for prompt in prompts:
        extractor.persist_and_accept(
            store,
            extractor.extract(ExtractionRequest(text=prompt)),
        )

    direct = DirectMemoryAnswerService()
    assert direct.answer(store, "where did i graduate from") == ("You graduated from BITS Pilani.")
    assert direct.answer(store, "what did i study in college") == (
        "You studied Bachelor of Engineering in Computer Science at BITS Pilani."
    )
    assert "playing chess" in (direct.answer(store, "what are my interests") or "")
    assert direct.answer(store, "who is my favourite chess player") == (
        "Your favorite chess player is Bobby Fischer."
    )
    assert direct.answer(store, "what programming languages do i prioritise") == (
        "Your programming-language priority is Python, C++, C, in that order."
    )
    assert direct.answer(store, "what game am i playing") == (
        "You are currently playing Ghost of Yotei."
    )


def test_current_activity_expires_and_archives_its_memory(store: MemoryStore) -> None:
    extractor = MemoryExtractionService()
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    extractor.persist_and_accept(
        store,
        extractor.extract(
            ExtractionRequest(
                text="i am currently playing ghost of yotei",
                source_timestamp=started_at,
                source_message_id=1,
            ),
        ),
    )

    assert store.archive_expired_activities(started_at + timedelta(days=31)) == 1
    assert store.list_activities(now=started_at + timedelta(days=31)) == []
    memory = store.list_memories(active_only=False)[0]
    assert memory.memory_type == MemoryType.ACTIVITY
    assert memory.status == "archived"


def test_only_user_authored_messages_are_extracted() -> None:
    result = MemoryExtractionService().extract(
        ExtractionRequest(
            messages=[
                ConversationMessage(role="user", content="Hello"),
                ConversationMessage(
                    role="assistant",
                    content="Your name is Invented Name and you love skiing.",
                ),
            ],
        ),
    )

    assert result.items == []


def test_typed_memory_migration_is_idempotent(tmp_path) -> None:
    database_path = tmp_path / "memory.db"
    database_url = f"sqlite+pysqlite:///{database_path}"

    initialize_database(database_url)
    initialize_database(database_url)

    engine = create_engine(database_url, future=True)
    inspector = inspect(engine)
    assert {"activities", "education", "memory_sources"} <= set(
        inspector.get_table_names(),
    )
    assert {"fingerprint", "expires_at"} <= {
        column["name"] for column in inspector.get_columns("memories")
    }
    assert {"target_date", "horizon_months", "fingerprint"} <= {
        column["name"] for column in inspector.get_columns("goals")
    }
    assert "detachment_reason" in {
        column["name"] for column in inspector.get_columns("memory_sources")
    }
    engine.dispose()


def test_model_only_memory_stays_pending_until_review(store: MemoryStore) -> None:
    class FakeLLM:
        def chat(self, *_args, **_kwargs) -> str:
            return (
                '{"items":[{"type":"preference","text":"chess",'
                '"confidence":0.99,"importance":5,'
                '"attributes":{"category":"interest","value":"chess"}}]}'
            )

    extractor = MemoryExtractionService()
    request = ExtractionRequest(text="I have a lasting interest in chess")
    extraction = extractor.extract_with_llm(request, FakeLLM())  # type: ignore[arg-type]
    candidates = extractor.persist_and_accept(store, extraction)

    assert len(candidates) == 1
    assert candidates[0].status == CandidateStatus.PENDING
    assert store.list_preferences() == []
    assert (
        extractor.format_persisted_acknowledgement(
            request,
            extraction,
            candidates,
        )
        is None
    )


def test_low_confidence_deterministic_candidate_stays_pending(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    extraction = extractor.extract(
        ExtractionRequest(
            text="I started an experiment",
            source_message_id=91,
        ),
    )

    candidates = extractor.persist_and_accept(store, extraction)

    assert len(candidates) == 1
    assert candidates[0].status == CandidateStatus.PENDING
    assert store.list_events() == []


def test_production_memory_path_uses_grounded_model_fallback_as_pending(
    store: MemoryStore,
) -> None:
    class FakeLLM:
        model = "fixture"

        def __init__(self) -> None:
            self.calls = 0

        def chat(self, *_args, **_kwargs) -> str:
            self.calls += 1
            return (
                '{"items":[{"type":"preference","text":"distributed systems",'
                '"source_span":"distributed systems","confidence":0.99,'
                '"importance":6,"attributes":{"category":"interest",'
                '"value":"distributed systems"}}]}'
            )

    llm = FakeLLM()
    service = object.__new__(NeoChatService)
    service.db = store.db
    service.store = store
    service.ollama = llm
    service.extractor = MemoryExtractionService()

    candidate_ids, acknowledgement = service.persist_user_memory(
        "I have a lasting interest in distributed systems",
        chat_id=None,
        source_message_id=92,
    )

    assert llm.calls == 1
    assert len(candidate_ids) == 1
    assert store.get_candidate(candidate_ids[0]).status == CandidateStatus.PENDING
    assert acknowledgement is None
    assert store.list_preferences() == []


def test_mixed_activity_and_live_question_never_becomes_a_name() -> None:
    extraction = MemoryExtractionService().extract(
        ExtractionRequest(
            text="I am playing Ghost of Yotei, when does it release?",
            source_message_id=55,
        ),
    )

    assert [item.candidate_type.value for item in extraction.items] == ["activity"]
    assert extraction.activities[0].attributes["activity"] == "playing Ghost of Yotei"
    assert all(item.attributes.get("key") != "name" for item in extraction.identity)
