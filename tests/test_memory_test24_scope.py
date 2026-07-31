from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import build_engine
from app.models.enums import CandidateStatus
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import ExtractionRequest, MemoryExtractionService
from app.services.memory_scope import domains_for_text
from app.services.retrieval import RetrievalRequest, RetrievalService

LANGUAGE_MEMORY = (
    "Remember this for future chats: I want to learn Spanish for travel conversations. "
    "I prefer language-learning advice in short daily practice routines with phrases, "
    "pronunciation tips, and review steps."
)
COOKING_MEMORY = (
    "Remember this too: I want to get better at simple vegetarian cooking. "
    "I prefer recipe advice with ingredients, exact cooking time, and beginner mistakes "
    "to avoid."
)
LANGUAGE_QUERY = (
    "What do you remember about my language-learning goals and preferences? "
    "Only use saved memory."
)
COOKING_QUERY = (
    "What do you remember about my cooking goals and recipe preferences? "
    "Only use saved memory."
)
BROAD_QUERY = (
    "What do you remember about my current goals and preferences? Only use saved memory."
)
PHOTOGRAPHY_MEMORY = (
    "Remember this for future chats: I want to get better at street photography. "
    "I prefer photography advice in short practice assignments with camera settings, "
    "composition tips, and review steps."
)
ORGANIZATION_MEMORY = (
    "Remember this too: I want to organize my room and desk better. "
    "I prefer home-organization advice with a checklist, time estimate, and what to "
    "throw away, keep, or store."
)
PHOTOGRAPHY_QUERY = (
    "What do you remember about my photography goals and photography advice preferences? "
    "Only use saved memory."
)
ORGANIZATION_QUERY = (
    "What do you remember about my room-organization goals and home-organization advice "
    "preferences? Only use saved memory."
)
CALLIGRAPHY_MEMORY = (
    "Remember this for future chats: I want to improve my brush calligraphy. "
    "I prefer calligraphy advice with stroke drills, pen angle notes, and review steps."
)
TRAVEL_MEMORY = (
    "Remember this too: I want to plan budget weekend trips better. "
    "I prefer travel-planning advice with budget range, transport options, and packing "
    "reminders."
)
CALLIGRAPHY_QUERY = (
    "What do you remember about my calligraphy goals and calligraphy advice preferences? "
    "Only use saved memory."
)
TRAVEL_QUERY = (
    "What do you remember about my budget travel goals and travel-planning advice "
    "preferences? Only use saved memory."
)


class MisclassifyingMemoryLLM:
    """Simulate provider variance that labels domain advice as response style."""

    model = "test-memory-model"

    def chat(self, messages, temperature=0.0) -> str:
        del temperature
        conversation = "\n".join(message.content for message in messages)
        fixtures = (
            (
                "home-organization advice",
                "organize my room and desk better",
                "Remember this too: I want to organize my room and desk better",
                (
                    "home-organization advice with a checklist, time estimate, and what "
                    "to throw away, keep, or store"
                ),
                (
                    "I prefer home-organization advice with a checklist, time estimate, "
                    "and what to throw away, keep, or store"
                ),
            ),
            (
                "photography advice",
                "get better at street photography",
                (
                    "Remember this for future chats: I want to get better at street "
                    "photography"
                ),
                (
                    "photography advice in short practice assignments with camera settings, "
                    "composition tips, and review steps"
                ),
                (
                    "I prefer photography advice in short practice assignments with camera "
                    "settings, composition tips, and review steps"
                ),
            ),
            (
                "travel-planning advice",
                "plan budget weekend trips better",
                "Remember this too: I want to plan budget weekend trips better",
                (
                    "travel-planning advice with budget range, transport options, and packing "
                    "reminders"
                ),
                (
                    "I prefer travel-planning advice with budget range, transport options, "
                    "and packing reminders"
                ),
            ),
            (
                "calligraphy advice",
                "improve my brush calligraphy",
                (
                    "Remember this for future chats: I want to improve my brush calligraphy"
                ),
                (
                    "calligraphy advice with stroke drills, pen angle notes, and review steps"
                ),
                (
                    "I prefer calligraphy advice with stroke drills, pen angle notes, and "
                    "review steps"
                ),
            ),
            (
                "vegetarian cooking",
                "get better at simple vegetarian cooking",
                (
                    "Remember this too: I want to get better at simple vegetarian cooking"
                ),
                (
                    "recipe advice with ingredients, exact cooking time, and beginner "
                    "mistakes to avoid"
                ),
                (
                    "I prefer recipe advice with ingredients, exact cooking time, and "
                    "beginner mistakes to avoid"
                ),
            ),
        )
        for marker, goal, goal_source, preference, preference_source in fixtures:
            if marker not in conversation:
                continue
            return self._response(
                goal=goal,
                goal_source=goal_source,
                preference=preference,
                preference_source=preference_source,
                duplicate_preference=True,
            )
        return self._response(
            goal="learn Spanish for travel conversations",
            goal_source=(
                "Remember this for future chats: I want to learn Spanish for travel conversations"
            ),
            preference=(
                "language-learning advice in short daily practice routines with phrases, "
                "pronunciation tips, and review steps"
            ),
            preference_source=(
                "I prefer language-learning advice in short daily practice routines with "
                "phrases, pronunciation tips, and review steps"
            ),
        )

    @staticmethod
    def _response(
        *,
        goal: str,
        goal_source: str,
        preference: str,
        preference_source: str,
        duplicate_preference: bool = False,
    ) -> str:
        items = [
            {
                "type": "goal",
                "text": goal,
                "source_span": goal_source,
                "declaration": "clear",
                "memory_subject": "user",
                "durability": "durable",
                "confidence": 0.95,
                "importance": 6,
                "attributes": {"goal": goal, "priority": 6},
            },
            {
                "type": "preference",
                "text": preference,
                "source_span": preference_source,
                "declaration": "clear",
                "memory_subject": "user",
                "durability": "durable",
                "confidence": 0.95,
                "importance": 6,
                "attributes": {
                    "category": "response_style",
                    "value": preference,
                    "canonical_slot": "preference:response_style",
                },
            },
        ]
        if duplicate_preference:
            items.append(
                {
                    **items[-1],
                    "attributes": {
                        **items[-1]["attributes"],
                        "category": "general",
                        "canonical_slot": "preference:general",
                    },
                }
            )
        return json.dumps({"items": items})


@pytest.fixture
def store(tmp_path) -> Iterator[MemoryStore]:
    engine = build_engine(f"sqlite:///{tmp_path / 'test24.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session: Session = factory()
    try:
        yield MemoryStore(session)
    finally:
        session.close()
        engine.dispose()


def _save_turn(store: MemoryStore, text: str) -> None:
    extractor = MemoryExtractionService()
    request = ExtractionRequest(text=text, persist=True)
    extraction = extractor.extract_with_llm(request, None)
    candidates = extractor.persist_and_accept(store, extraction)
    store.db.commit()
    assert len(candidates) == 2
    assert all(candidate.status == CandidateStatus.ACCEPTED for candidate in candidates)


@pytest.fixture
def populated_store(store: MemoryStore) -> MemoryStore:
    _save_turn(store, LANGUAGE_MEMORY)
    _save_turn(store, COOKING_MEMORY)
    assert len(store.list_memories(limit=100)) == 4
    return store


def test_manual_replay_survives_model_misclassification_without_cross_domain_update(
    store: MemoryStore,
) -> None:
    service = NeoChatService(store.db, ollama=MisclassifyingMemoryLLM())

    language_result = service.persist_user_memory(LANGUAGE_MEMORY)
    cooking_result = service.persist_user_memory(COOKING_MEMORY)

    assert language_result.saved_count == 2
    assert language_result.updated_count == 0
    assert cooking_result.saved_count == 2
    assert cooking_result.updated_count == 0

    memories = store.list_memories(limit=100)
    preferences = store.list_preferences()
    assert len(memories) == 4
    assert len(preferences) == 2
    assert {preference.category for preference in preferences} == {
        "language_learning",
        "cooking",
    }
    assert {preference.canonical_slot for preference in preferences} == {
        "preference:language_learning",
        "preference:cooking",
    }
    assert all(preference.category != "response_style" for preference in preferences)
    assert all(
        not memory.memory_text.startswith("response_style =")
        for memory in memories
    )

    language_answer = service.direct_answers.answer(store, LANGUAGE_QUERY)
    cooking_answer = service.direct_answers.answer(store, COOKING_QUERY)
    broad_answer = service.direct_answers.answer(store, BROAD_QUERY)

    assert language_answer is not None
    assert cooking_answer is not None
    assert broad_answer is not None
    assert language_answer.count("\n- ") == 2
    assert cooking_answer.count("\n- ") == 2
    assert broad_answer.count("\n- ") == 4
    assert "vegetarian cooking" not in language_answer
    assert "recipe advice" not in language_answer
    assert "Spanish" not in cooking_answer
    assert "language-learning advice" not in cooking_answer


@pytest.mark.parametrize(
    (
        "first_memory",
        "second_memory",
        "first_query",
        "second_query",
        "first_domain",
        "second_domain",
        "first_terms",
        "second_terms",
        "first_advice_query",
        "second_advice_query",
    ),
    [
        (
            PHOTOGRAPHY_MEMORY,
            ORGANIZATION_MEMORY,
            PHOTOGRAPHY_QUERY,
            ORGANIZATION_QUERY,
            "photography",
            "home_organization",
            ("street photography", "camera settings", "composition tips"),
            ("organize my room", "checklist", "throw away"),
            "Give me a beginner street photography practice plan for tomorrow.",
            "Give me a simple plan to organize my desk tomorrow.",
        ),
        (
            CALLIGRAPHY_MEMORY,
            TRAVEL_MEMORY,
            CALLIGRAPHY_QUERY,
            TRAVEL_QUERY,
            "calligraphy",
            "travel_planning",
            ("brush calligraphy", "stroke drills", "pen angle"),
            ("budget weekend trips", "transport options", "packing reminders"),
            "Give me a beginner brush calligraphy practice plan for tomorrow.",
            "Give me a simple budget weekend travel plan.",
        ),
    ],
)
def test_universal_domain_replay_isolates_arbitrary_topic_pairs(
    store: MemoryStore,
    first_memory: str,
    second_memory: str,
    first_query: str,
    second_query: str,
    first_domain: str,
    second_domain: str,
    first_terms: tuple[str, ...],
    second_terms: tuple[str, ...],
    first_advice_query: str,
    second_advice_query: str,
) -> None:
    service = NeoChatService(store.db, ollama=MisclassifyingMemoryLLM())

    first_result = service.persist_user_memory(first_memory)
    second_result = service.persist_user_memory(second_memory)

    assert (first_result.saved_count, first_result.updated_count) == (2, 0)
    assert (second_result.saved_count, second_result.updated_count) == (2, 0)

    memories = store.list_memories(limit=100)
    preferences = store.list_preferences()
    assert len(memories) == 4
    assert len(preferences) == 2
    assert {preference.category for preference in preferences} == {
        first_domain,
        second_domain,
    }
    assert {preference.canonical_slot for preference in preferences} == {
        f"preference:{first_domain}",
        f"preference:{second_domain}",
    }
    assert sum(memory.canonical_slot.startswith("goal:") for memory in memories) == 2
    assert all(memory.status == "active" and memory.is_active for memory in memories)

    first_answer = service.direct_answers.answer(store, first_query)
    second_answer = service.direct_answers.answer(store, second_query)
    broad_answer = service.direct_answers.answer(store, BROAD_QUERY)

    assert first_answer is not None
    assert second_answer is not None
    assert broad_answer is not None
    assert first_answer.count("\n- ") == 2
    assert second_answer.count("\n- ") == 2
    assert broad_answer.count("\n- ") == 4
    assert all(term.lower() in first_answer.lower() for term in first_terms)
    assert all(term.lower() not in first_answer.lower() for term in second_terms)
    assert all(term.lower() in second_answer.lower() for term in second_terms)
    assert all(term.lower() not in second_answer.lower() for term in first_terms)
    assert all(term.lower() in broad_answer.lower() for term in (*first_terms, *second_terms))

    first_context = RetrievalService().retrieve(
        store,
        RetrievalRequest(query=first_advice_query, include_archives=False),
    )
    second_context = RetrievalService().retrieve(
        store,
        RetrievalRequest(query=second_advice_query, include_archives=False),
    )
    unrelated_context = RetrievalService().retrieve(
        store,
        RetrievalRequest(
            query="Explain how hash tables handle collisions.",
            include_archives=False,
        ),
    )

    first_rendered = _render_retrieval(first_context)
    second_rendered = _render_retrieval(second_context)
    assert all(term.lower() in first_rendered.lower() for term in first_terms)
    assert all(term.lower() not in first_rendered.lower() for term in second_terms)
    assert all(term.lower() in second_rendered.lower() for term in second_terms)
    assert all(term.lower() not in second_rendered.lower() for term in first_terms)
    assert _render_retrieval(unrelated_context) == ""


def test_shared_advice_format_cannot_merge_different_unseen_domains(
    store: MemoryStore,
) -> None:
    service = NeoChatService(store.db, ollama=None)
    pottery = (
        "Remember this: I want to get better at wheel pottery. "
        "I prefer pottery advice in short daily practice assignments with composition tips "
        "and review steps."
    )
    calligraphy = (
        "Remember this too: I want to improve my brush calligraphy. "
        "I prefer calligraphy advice in short daily practice assignments with composition "
        "tips and review steps."
    )

    pottery_result = service.persist_user_memory(pottery)
    calligraphy_result = service.persist_user_memory(calligraphy)

    assert (pottery_result.saved_count, pottery_result.updated_count) == (2, 0)
    assert (calligraphy_result.saved_count, calligraphy_result.updated_count) == (2, 0)
    assert {preference.category for preference in store.list_preferences()} == {
        "pottery",
        "calligraphy",
    }
    assert len(store.list_memories(limit=100)) == 4


def _render_retrieval(result) -> str:
    return " ".join(
        [
            *(goal.goal for goal in result.goals),
            *(preference.value for preference in result.preferences),
            *(memory.memory_text for memory in result.relevant_memories),
        ]
    )


@pytest.mark.parametrize(
    ("prompt", "category", "slot"),
    [
        (LANGUAGE_MEMORY, "language_learning", "preference:language_learning"),
        (COOKING_MEMORY, "cooking", "preference:cooking"),
        (PHOTOGRAPHY_MEMORY, "photography", "preference:photography"),
        (
            ORGANIZATION_MEMORY,
            "home_organization",
            "preference:home_organization",
        ),
        (CALLIGRAPHY_MEMORY, "calligraphy", "preference:calligraphy"),
        (TRAVEL_MEMORY, "travel_planning", "preference:travel_planning"),
    ],
)
def test_domain_preferences_have_stable_non_response_style_categories(
    prompt: str,
    category: str,
    slot: str,
) -> None:
    extraction = MemoryExtractionService().extract(ExtractionRequest(text=prompt))

    assert len(extraction.preferences) == 1
    preference = extraction.preferences[0]
    assert preference.attributes["category"] == category
    assert preference.attributes["canonical_slot"] == slot
    assert preference.attributes["category"] != "response_style"
    assert preference.attributes["memory_kind"] == "preference"
    assert preference.attributes["domain"] == category
    assert preference.attributes["scope"] == "domain_specific"
    assert preference.attributes["canonical_text"] == preference.attributes["value"]
    assert category in str(preference.attributes["domain_keywords"])


def test_scoped_language_memory_only_answer_excludes_cooking(
    populated_store: MemoryStore,
) -> None:
    answer = DirectMemoryAnswerService().answer(populated_store, LANGUAGE_QUERY)

    assert answer is not None
    assert "Spanish" in answer
    assert "language-learning advice" in answer
    for excluded in (
        "vegetarian cooking",
        "recipe advice",
        "ingredients",
        "cooking time",
        "beginner mistakes",
    ):
        assert excluded not in answer.lower()


def test_scoped_cooking_memory_only_answer_excludes_language(
    populated_store: MemoryStore,
) -> None:
    answer = DirectMemoryAnswerService().answer(populated_store, COOKING_QUERY)

    assert answer is not None
    assert "vegetarian cooking" in answer
    assert "recipe advice" in answer
    for excluded in (
        "spanish",
        "travel conversations",
        "phrases",
        "pronunciation",
        "review steps",
    ):
        assert excluded not in answer.lower()


def test_broad_memory_only_answer_still_returns_every_domain(
    populated_store: MemoryStore,
) -> None:
    answer = DirectMemoryAnswerService().answer(populated_store, BROAD_QUERY)

    assert answer is not None
    assert "Spanish" in answer
    assert "language-learning advice" in answer
    assert "vegetarian cooking" in answer
    assert "recipe advice" in answer


@pytest.mark.parametrize(
    ("query", "included", "excluded"),
    [
        (
            "Create a short Spanish study plan for me.",
            ("Spanish", "language-learning advice"),
            ("cooking", "recipe advice"),
        ),
        (
            "Suggest a simple vegetarian recipe for me.",
            ("vegetarian cooking", "recipe advice"),
            ("Spanish", "language-learning"),
        ),
    ],
)
def test_normal_advice_context_contains_only_compatible_domain(
    populated_store: MemoryStore,
    query: str,
    included: tuple[str, ...],
    excluded: tuple[str, ...],
) -> None:
    result = RetrievalService().retrieve(
        populated_store,
        RetrievalRequest(query=query, include_archives=False),
    )
    rendered = " ".join(
        [
            *(goal.goal for goal in result.goals),
            *(preference.value for preference in result.preferences),
            *(memory.memory_text for memory in result.relevant_memories),
        ]
    )

    assert all(value in rendered for value in included)
    assert all(value.lower() not in rendered.lower() for value in excluded)


def test_unrelated_technical_answer_injects_neither_domain(
    populated_store: MemoryStore,
) -> None:
    result = RetrievalService().retrieve(
        populated_store,
        RetrievalRequest(
            query="Recommend a clear way to explain binary search tree insertion.",
            include_archives=False,
        ),
    )

    assert result.goals == []
    assert result.preferences == []
    assert result.relevant_memories == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What are my Spanish learning goals?", {"language_learning"}),
        ("What recipe advice do I prefer?", {"cooking"}),
        ("What are my workout preferences?", {"fitness"}),
        ("What music practice routine do I prefer?", {"music"}),
        ("What plant care advice do I prefer?", {"gardening"}),
        ("What technical explanations do I prefer?", {"coding"}),
        ("What photography advice do I prefer?", {"photography"}),
        ("What calligraphy advice do I prefer?", {"calligraphy"}),
        ("What are my room-organization goals?", {"home_organization"}),
        ("What are my budget travel plans?", {"travel_planning"}),
        ("What are all my goals and preferences?", set()),
    ],
)
def test_universal_domain_detection_includes_normalized_or_unseen_topic(
    query: str,
    expected: set[str],
) -> None:
    domains = domains_for_text(query)
    if expected:
        assert expected <= domains
    else:
        assert domains == frozenset()


def test_only_explicitly_global_answer_preferences_get_global_scope() -> None:
    extraction = MemoryExtractionService().extract(
        ExtractionRequest(
            text=(
                "I prefer all answers to be concise and direct. "
                "I prefer calligraphy advice with stroke drills."
            )
        )
    )

    assert len(extraction.preferences) == 2
    global_preference = next(
        item
        for item in extraction.preferences
        if item.attributes["category"] == "response_style"
    )
    calligraphy_preference = next(
        item
        for item in extraction.preferences
        if item.attributes["category"] == "calligraphy"
    )
    assert global_preference.attributes["scope"] == "global"
    assert global_preference.attributes["memory_kind"] == "response_style"
    assert calligraphy_preference.attributes["scope"] == "domain_specific"
    assert calligraphy_preference.attributes["memory_kind"] == "preference"
