from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401
from app.db.base import Base
from app.db.session import build_engine
from app.models.enums import CandidateStatus, GoalStatus, MemoryType
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.extraction import ExtractionRequest, MemoryExtractionService
from app.services.memory_scope import domains_for_text, text_matches_domains
from app.services.retrieval import RetrievalRequest, RetrievalService

PUBLIC_SPEAKING_INITIAL = (
    "Remember this for future chats: I want to improve at long-form technical "
    "presentations. I prefer public-speaking advice in detailed 30-minute practice "
    "sessions with scripts, timing, and slide notes."
)
PUBLIC_SPEAKING_CORRECTION = (
    "Correction: replace my public-speaking goal and preference. My current "
    "public-speaking goal is to give short interview-style answers clearly, not "
    "long-form technical presentations. I now prefer public-speaking advice in "
    "concise 5-minute drills with one prompt, one model answer, and one self-check, "
    "not scripts, timing, or slide notes."
)
WATERCOLOR_INITIAL = (
    "Remember this for future chats: I want to improve at watercolor landscapes. "
    "I prefer watercolor advice in long weekend studies with reference images, color "
    "swatches, and critique notes."
)
WATERCOLOR_CORRECTION = (
    "Correction: replace my watercolor goal and preference. My current watercolor "
    "goal is to paint simple loose florals confidently, not watercolor landscapes. "
    "I now prefer watercolor advice in 10-minute daily exercises with one brush "
    "technique, one color mix, and one self-review, not weekend studies, reference "
    "images, color swatches, or critique notes."
)
WRITING_INITIAL = (
    "Remember this for future chats: I want to improve at long-form blog essays. "
    "I prefer writing advice in detailed 45-minute drafting sessions with outlines, "
    "research notes, and revision checklists."
)
WRITING_CORRECTION = (
    "Correction: replace my writing goal and preference. My current writing goal is "
    "to write concise LinkedIn posts clearly, not long-form blog essays. I now prefer "
    "writing advice in quick 10-minute drills with one hook, one draft, and one edit, "
    "not outlines, research notes, or revision checklists."
)
ASTRONOMY_INITIAL = (
    "Remember this for future chats: I want to improve at backyard astronomy "
    "observations. I prefer astronomy advice in detailed 60-minute sessions with star "
    "charts, equipment notes, and observation logs."
)
ASTRONOMY_CORRECTION = (
    "Correction: replace my astronomy goal and preference. My current astronomy goal "
    "is to identify constellations quickly, not backyard astronomy observations. I now "
    "prefer astronomy advice in quick 8-minute sky checks with one target, one note, and "
    "one sketch, not star charts, equipment notes, or observation logs."
)
VIDEO_EDITING_INITIAL = (
    "Remember this for future chats: I want to improve at long-form cinematic YouTube "
    "videos. I prefer video-editing advice in detailed 60-minute editing sessions with "
    "storyboards, color grading notes, and sound design checklists."
)
VIDEO_EDITING_CORRECTION = (
    "Correction: replace my video-editing goal and preference. My current video-editing "
    "goal is to create short Instagram reels clearly, not long-form cinematic YouTube "
    "videos. I now prefer video-editing advice in quick 15-minute drills with one hook, "
    "one cut, and one caption, not storyboards, color grading notes, or sound design "
    "checklists."
)


class FragmentingCorrectionLLM:
    """Reproduce a provider splitting positive and negated replacement clauses."""

    model = "test-fragmenting-memory-model"

    def chat(self, messages, temperature=0.0) -> str:
        del temperature
        conversation = "\n".join(message.content for message in messages)
        if "Correction: replace my writing goal and preference" not in conversation:
            return json.dumps({"items": []})
        return json.dumps(
            {
                "items": [
                    {
                        "type": "goal",
                        "text": "write concise LinkedIn posts clearly",
                        "source_span": "write concise LinkedIn posts clearly",
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "goal": "write concise LinkedIn posts clearly",
                            "priority": 6,
                        },
                    },
                    {
                        "type": "preference",
                        "text": (
                            "quick 10-minute drills with one hook, one draft, and one edit"
                        ),
                        "source_span": (
                            "quick 10-minute drills with one hook, one draft, and one edit"
                        ),
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "category": "response_style",
                            "value": (
                                "quick 10-minute drills with one hook, one draft, and one edit"
                            ),
                            "canonical_slot": "preference:response_style",
                        },
                    },
                    {
                        "type": "preference",
                        "text": "not long-form blog essays",
                        "source_span": "not long-form blog essays",
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "category": "general",
                            "value": "not long-form blog essays",
                            "canonical_slot": "preference:general",
                        },
                    },
                    {
                        "type": "preference",
                        "text": (
                            "not outlines, research notes, or revision checklists"
                        ),
                        "source_span": (
                            "not outlines, research notes, or revision checklists"
                        ),
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "category": "writing",
                            "value": (
                                "not outlines, research notes, or revision checklists"
                            ),
                            "canonical_slot": "preference:writing",
                        },
                    },
                ]
            }
        )


class ObservedVideoEditingCorrectionLLM:
    """Reproduce the malformed provider output observed in the Test 25C replay."""

    model = "test-video-editing-memory-model"

    def chat(self, messages, temperature=0.0) -> str:
        del temperature
        conversation = "\n".join(message.content for message in messages)
        if "Correction: replace my video-editing goal and preference" not in conversation:
            return json.dumps({"items": []})
        return json.dumps(
            {
                "items": [
                    {
                        "type": "goal",
                        "text": (
                            "create short Instagram reels clearly, not long-form cinematic "
                            "YouTube videos"
                        ),
                        "source_span": (
                            "create short Instagram reels clearly, not long-form cinematic "
                            "YouTube videos"
                        ),
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "goal": (
                                "create short Instagram reels clearly, not long-form "
                                "cinematic YouTube videos"
                            ),
                            "priority": 6,
                        },
                    },
                    {
                        "type": "preference",
                        "text": (
                            "quick 15-minute drills with one hook, one cut, and one caption"
                        ),
                        "source_span": (
                            "quick 15-minute drills with one hook, one cut, and one caption"
                        ),
                        "declaration": "clear",
                        "memory_subject": "user",
                        "durability": "durable",
                        "confidence": 0.95,
                        "importance": 6,
                        "attributes": {
                            "category": "response_style",
                            "value": (
                                "quick 15-minute drills with one hook, one cut, and one "
                                "caption"
                            ),
                            "canonical_slot": "preference:response_style",
                        },
                    },
                ]
            }
        )


@pytest.fixture
def store(tmp_path) -> Iterator[MemoryStore]:
    engine = build_engine(f"sqlite:///{tmp_path / 'test25.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session: Session = factory()
    try:
        yield MemoryStore(session)
    finally:
        session.close()
        engine.dispose()


@pytest.mark.parametrize(
    (
        "initial",
        "correction",
        "query_domain",
        "canonical_domain",
        "new_goal",
        "new_preference",
        "old_terms",
        "advice_query",
    ),
    [
        (
            PUBLIC_SPEAKING_INITIAL,
            PUBLIC_SPEAKING_CORRECTION,
            "public-speaking",
            "public_speaking",
            "give short interview-style answers clearly",
            "public-speaking advice in concise 5-minute drills with one prompt, one model "
            "answer, and one self-check",
            (
                "long-form technical presentations",
                "30-minute practice sessions",
                "scripts",
                "timing",
                "slide notes",
            ),
            "Give me a public-speaking practice plan.",
        ),
        (
            WATERCOLOR_INITIAL,
            WATERCOLOR_CORRECTION,
            "watercolor",
            "watercolor",
            "paint simple loose florals confidently",
            "watercolor advice in 10-minute daily exercises with one brush technique, one "
            "color mix, and one self-review",
            (
                "watercolor landscapes",
                "weekend studies",
                "reference images",
                "color swatches",
                "critique notes",
            ),
            "Give me a watercolor practice plan.",
        ),
        (
            WRITING_INITIAL,
            WRITING_CORRECTION,
            "writing",
            "writing",
            "write concise LinkedIn posts clearly",
            "writing advice in quick 10-minute drills with one hook, one draft, and one edit",
            (
                "long-form blog essays",
                "45-minute drafting sessions",
                "outlines",
                "research notes",
                "revision checklists",
            ),
            "Give me a writing practice plan.",
        ),
        (
            ASTRONOMY_INITIAL,
            ASTRONOMY_CORRECTION,
            "astronomy",
            "astronomy",
            "identify constellations quickly",
            "astronomy advice in quick 8-minute sky checks with one target, one note, and "
            "one sketch",
            (
                "backyard astronomy observations",
                "60-minute sessions",
                "star charts",
                "equipment notes",
                "observation logs",
            ),
            "Give me an astronomy practice plan.",
        ),
        (
            VIDEO_EDITING_INITIAL,
            VIDEO_EDITING_CORRECTION,
            "video-editing",
            "video_editing",
            "create short Instagram reels clearly",
            "video-editing advice in quick 15-minute drills with one hook, one cut, and "
            "one caption",
            (
                "long-form cinematic youtube videos",
                "60-minute editing sessions",
                "storyboards",
                "color grading notes",
                "sound design checklists",
            ),
            "Give me a video-editing practice plan.",
        ),
    ],
)
def test_explicit_same_domain_correction_supersedes_old_goal_and_preference(
    store: MemoryStore,
    initial: str,
    correction: str,
    query_domain: str,
    canonical_domain: str,
    new_goal: str,
    new_preference: str,
    old_terms: tuple[str, ...],
    advice_query: str,
) -> None:
    service = NeoChatService(store.db, ollama=None)

    initial_result = service.persist_user_memory(initial)
    assert (initial_result.saved_count, initial_result.updated_count) == (2, 0)

    initial_recall = DirectMemoryAnswerService().answer(
        store,
        f"What do you remember about my {query_domain} goals and advice preferences? "
        "Only use saved memory.",
    )
    assert initial_recall is not None
    assert initial_recall.count("\n- ") == 2
    assert all(term in initial_recall.lower() for term in old_terms)

    correction_result = service.persist_user_memory(correction)
    assert (correction_result.saved_count, correction_result.updated_count) == (0, 2)
    assert correction_result.acknowledgement() == (
        "Updated 2 durable memories after extraction and review."
    )

    active_memories = store.list_memories(limit=100)
    assert len(active_memories) == 2
    assert {memory.memory_text for memory in active_memories} == {
        new_goal,
        f"{canonical_domain} = {new_preference}",
    }
    assert all(memory.is_active and memory.status == "active" for memory in active_memories)
    assert all(
        not any(term in memory.memory_text.lower() for term in old_terms)
        for memory in active_memories
    )
    assert {memory.canonical_slot.split(":", maxsplit=2)[1] for memory in active_memories} == {
        canonical_domain
    }

    inactive_memories = store.list_memories(active_only=False, limit=100)
    superseded = [memory for memory in inactive_memories if memory.status == "superseded"]
    assert len(superseded) == 2
    assert all(not memory.is_active for memory in superseded)
    assert any(memory.memory_type == MemoryType.GOAL_RELATED for memory in superseded)
    assert any(memory.memory_type == MemoryType.PREFERENCE for memory in superseded)
    assert all(
        any(term in memory.memory_text.lower() for term in old_terms)
        for memory in superseded
    )

    goals = store.list_goals()
    assert [goal.goal for goal in store.list_goals(GoalStatus.ACTIVE)] == [new_goal]
    assert any(goal.status == GoalStatus.ABANDONED for goal in goals)
    assert len(store.list_preferences()) == 1
    assert store.list_preferences()[0].value == new_preference

    scoped_recall = DirectMemoryAnswerService().answer(
        store,
        f"What do you remember about my {query_domain} goals and advice preferences? "
        "Only use saved memory.",
    )
    broad_recall = DirectMemoryAnswerService().answer(
        store,
        "What do you remember about my current goals and preferences? Only use saved memory.",
    )
    assert scoped_recall is not None
    assert broad_recall is not None
    for answer in (scoped_recall, broad_recall):
        assert new_goal in answer
        assert new_preference in answer
        assert all(term not in answer.lower() for term in old_terms)

    context = RetrievalService().retrieve(
        store,
        RetrievalRequest(query=advice_query, include_archives=False),
    )
    rendered_context = " ".join(
        [
            *(goal.goal for goal in context.goals),
            *(preference.value for preference in context.preferences),
            *(memory.memory_text for memory in context.relevant_memories),
        ]
    )
    assert [goal.goal for goal in context.goals] == [new_goal]
    assert [preference.value for preference in context.preferences] == [new_preference]
    assert new_goal in rendered_context
    assert new_preference in rendered_context
    assert all(term not in rendered_context.lower() for term in old_terms)

    resurrection = service.persist_user_memory(initial)
    assert (resurrection.saved_count, resurrection.updated_count) == (0, 0)
    assert resurrection.review_decisions == [
        CandidateStatus.REJECTED.value,
        CandidateStatus.REJECTED.value,
    ]
    assert all(
        term not in memory.memory_text.lower()
        for memory in store.list_memories()
        for term in old_terms
    )


def test_fragmented_writing_correction_is_canonicalized_before_review(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    extraction = extractor.extract_with_llm(
        ExtractionRequest(text=WRITING_CORRECTION),
        FragmentingCorrectionLLM(),
    )

    assert len(extraction.items) == 2
    assert len(extraction.goals) == 1
    assert len(extraction.preferences) == 1
    goal = extraction.goals[0]
    preference = extraction.preferences[0]
    assert goal.text == "write concise LinkedIn posts clearly"
    assert goal.attributes["domain"] == "writing"
    assert goal.attributes["replacement_intent"] == 1
    assert goal.attributes["canonical_slot"] == (
        "goal:writing:write_concise_linkedin_posts_clearly"
    )
    assert preference.attributes["category"] == "writing"
    assert preference.attributes["value"] == (
        "writing advice in quick 10-minute drills with one hook, one draft, and one edit"
    )
    assert preference.attributes["memory_kind"] == "preference"
    assert preference.attributes["domain"] == "writing"
    assert preference.attributes["scope"] == "domain_specific"
    assert preference.attributes["canonical_slot"] == "preference:writing"
    assert preference.attributes["replacement_intent"] == 1
    assert all(
        not item.text.casefold().startswith(("not ", "response_style"))
        for item in extraction.items
    )

    service = NeoChatService(store.db, ollama=FragmentingCorrectionLLM())
    initial_result = service.persist_user_memory(WRITING_INITIAL)
    correction_result = service.persist_user_memory(WRITING_CORRECTION)

    assert (initial_result.saved_count, initial_result.updated_count) == (2, 0)
    assert (correction_result.saved_count, correction_result.updated_count) == (0, 2)
    assert correction_result.candidate_count == 2
    assert correction_result.review_decisions == [
        CandidateStatus.ACCEPTED.value,
        CandidateStatus.ACCEPTED.value,
    ]
    assert correction_result.acknowledgement() == (
        "Updated 2 durable memories after extraction and review."
    )

    expected_active = {
        "write concise LinkedIn posts clearly",
        (
            "writing = writing advice in quick 10-minute drills with one hook, one draft, "
            "and one edit"
        ),
    }
    active_memories = store.list_memories(limit=100)
    assert {memory.memory_text for memory in active_memories} == expected_active
    assert {memory.canonical_slot for memory in active_memories} == {
        "goal:writing:write_concise_linkedin_posts_clearly",
        "preference:writing",
    }
    forbidden = (
        "long-form blog essays",
        "45-minute drafting sessions",
        "outlines",
        "research notes",
        "revision checklists",
        "not long-form",
        "not outlines",
    )
    assert all(
        term not in memory.memory_text.casefold()
        for memory in active_memories
        for term in forbidden
    )

    all_memories = store.list_memories(active_only=False, limit=100)
    assert len([memory for memory in all_memories if memory.status == "superseded"]) == 2
    broad_recall = DirectMemoryAnswerService().answer(
        store,
        "What do you remember about my current goals and preferences? Only use saved memory.",
    )
    assert broad_recall is not None
    assert "Goal: write concise LinkedIn posts clearly" in broad_recall
    assert (
        "Preference: writing advice in quick 10-minute drills with one hook, one draft, "
        "and one edit"
    ) in broad_recall
    assert all(term not in broad_recall.casefold() for term in forbidden)

    active_ids = {memory.id for memory in active_memories}
    repeated_result = service.persist_user_memory(WRITING_CORRECTION)
    assert (repeated_result.saved_count, repeated_result.updated_count) == (0, 2)
    assert {memory.id for memory in store.list_memories(limit=100)} == active_ids
    assert len(store.list_preferences()) == 1
    assert len(store.list_goals(GoalStatus.ACTIVE)) == 1


def test_observed_video_editing_model_output_is_replaced_by_positive_pair(
    store: MemoryStore,
) -> None:
    extractor = MemoryExtractionService()
    extraction = extractor.extract_with_llm(
        ExtractionRequest(text=VIDEO_EDITING_CORRECTION),
        ObservedVideoEditingCorrectionLLM(),
    )

    assert len(extraction.goals) == 1
    assert len(extraction.preferences) == 1
    assert extraction.goals[0].text == "create short Instagram reels clearly"
    assert extraction.goals[0].attributes["domain"] == "video_editing"
    assert extraction.goals[0].attributes["canonical_slot"] == (
        "goal:video_editing:create_short_instagram_reels_clearly"
    )
    preference = extraction.preferences[0]
    assert preference.attributes["category"] == "video_editing"
    assert preference.attributes["domain"] == "video_editing"
    assert preference.attributes["scope"] == "domain_specific"
    assert preference.attributes["canonical_slot"] == "preference:video_editing"
    assert preference.attributes["value"] == (
        "video-editing advice in quick 15-minute drills with one hook, one cut, and "
        "one caption"
    )
    assert all(
        "not " not in str(
            item.attributes.get("goal") or item.attributes.get("value") or item.text
        ).casefold()
        for item in extraction.items
    )

    service = NeoChatService(store.db, ollama=ObservedVideoEditingCorrectionLLM())
    initial_result = service.persist_user_memory(VIDEO_EDITING_INITIAL)
    correction_result = service.persist_user_memory(VIDEO_EDITING_CORRECTION)

    assert (initial_result.saved_count, initial_result.updated_count) == (2, 0)
    assert (correction_result.saved_count, correction_result.updated_count) == (0, 2)
    assert correction_result.candidate_count == 2
    assert correction_result.review_decisions == [
        CandidateStatus.ACCEPTED.value,
        CandidateStatus.ACCEPTED.value,
    ]
    assert correction_result.acknowledgement() == (
        "Updated 2 durable memories after extraction and review."
    )
    active_memories = store.list_memories(limit=100)
    assert {memory.memory_text for memory in active_memories} == {
        "create short Instagram reels clearly",
        (
            "video_editing = video-editing advice in quick 15-minute drills with one "
            "hook, one cut, and one caption"
        ),
    }
    assert {memory.canonical_slot for memory in active_memories} == {
        "goal:video_editing:create_short_instagram_reels_clearly",
        "preference:video_editing",
    }
    assert len(
        [
            memory
            for memory in store.list_memories(active_only=False, limit=100)
            if memory.status == "superseded"
        ]
    ) == 2

    active_ids = {memory.id for memory in active_memories}
    typed_preference = store.list_preferences()[0]
    typed_preference.category = "editing"
    typed_preference.canonical_slot = "preference:editing"
    preference_memory = next(
        memory
        for memory in active_memories
        if memory.memory_type == MemoryType.PREFERENCE
    )
    preference_memory.memory_text = preference_memory.memory_text.replace(
        "video_editing =",
        "editing =",
    )
    preference_memory.canonical_slot = "preference:editing"
    store.db.commit()

    repeated_result = service.persist_user_memory(VIDEO_EDITING_CORRECTION)
    assert (repeated_result.saved_count, repeated_result.updated_count) == (0, 2)
    assert {memory.id for memory in store.list_memories(limit=100)} == active_ids
    assert len(store.list_preferences()) == 1
    assert store.list_preferences()[0].canonical_slot == "preference:video_editing"
    assert {
        memory.canonical_slot for memory in store.list_memories(limit=100)
    } == {
        "goal:video_editing:create_short_instagram_reels_clearly",
        "preference:video_editing",
    }


@pytest.mark.parametrize(
    "boundary",
    ["not", "instead of", "rather than", "no longer"],
)
@pytest.mark.parametrize("separator", [", ", "—"])
def test_every_replacement_boundary_keeps_only_the_positive_values(
    boundary: str,
    separator: str,
) -> None:
    correction = (
        "Correction: replace my ceramic-glazing goal and preference. My current "
        "ceramic-glazing goal is to fire small test tiles consistently"
        f"{separator}"
        f"{boundary} large decorative vases. I now prefer ceramic-glazing advice in "
        f"quick test batches{separator}{boundary} long studio sessions."
    )

    extraction = MemoryExtractionService().extract(ExtractionRequest(text=correction))

    assert len(extraction.goals) == 1
    assert len(extraction.preferences) == 1
    assert extraction.goals[0].text == "fire small test tiles consistently"
    assert extraction.preferences[0].attributes["value"] == (
        "ceramic-glazing advice in quick test batches"
    )
    assert {
        extraction.goals[0].attributes["domain"],
        extraction.preferences[0].attributes["domain"],
    } == {"ceramic_glazing"}
    assert extraction.goals[0].attributes["negated_fragments"] == "large decorative vases"
    assert (
        extraction.preferences[0].attributes["negated_fragments"]
        == "long studio sessions"
    )


def test_non_replacement_not_only_goal_is_not_truncated() -> None:
    extraction = MemoryExtractionService().extract(
        ExtractionRequest(
            text="Remember this: I want to improve at not only Python but also Rust."
        )
    )

    assert len(extraction.goals) == 1
    assert extraction.goals[0].text == "improve at not only Python but also Rust"


def test_compound_domains_with_the_same_head_keep_independent_slots(
    store: MemoryStore,
) -> None:
    service = NeoChatService(store.db, ollama=None)
    video_result = service.persist_user_memory(
        "Remember this: I want to improve at documentary pacing. I prefer "
        "video-editing advice in short timeline exercises."
    )
    photo_result = service.persist_user_memory(
        "Remember this: I want to improve at portrait retouching. I prefer "
        "photo-editing advice in short masking exercises."
    )

    assert (video_result.saved_count, video_result.updated_count) == (2, 0)
    assert (photo_result.saved_count, photo_result.updated_count) == (2, 0)
    assert {preference.canonical_slot for preference in store.list_preferences()} == {
        "preference:video_editing",
        "preference:photo_editing",
    }
    assert len(store.list_memories(limit=100)) == 4

    scoped_recall = DirectMemoryAnswerService().answer(
        store,
        "What do you remember about my video-editing goals and advice preferences? "
        "Only use saved memory.",
    )
    assert scoped_recall is not None
    assert "documentary pacing" in scoped_recall
    assert "short timeline exercises" in scoped_recall
    assert "portrait retouching" not in scoped_recall
    assert "short masking exercises" not in scoped_recall

    context = RetrievalService().retrieve(
        store,
        RetrievalRequest(
            query="Give me a video-editing practice plan.",
            include_archives=False,
        ),
    )
    rendered_context = " ".join(
        [
            *(goal.goal for goal in context.goals),
            *(preference.value for preference in context.preferences),
            *(memory.memory_text for memory in context.relevant_memories),
        ]
    )
    assert "documentary pacing" in rendered_context
    assert "short timeline exercises" in rendered_context
    assert "portrait retouching" not in rendered_context
    assert "short masking exercises" not in rendered_context


def test_compound_scope_keeps_explicit_simple_domains_in_a_mixed_query() -> None:
    requested = domains_for_text(
        "What are my video-editing and cooking preferences?"
    )

    assert text_matches_domains(
        "preference:video_editing video-editing advice in timeline exercises",
        requested,
    )
    assert text_matches_domains(
        "preference:cooking cooking = vegetarian recipes",
        requested,
    )
    assert not text_matches_domains(
        "preference:photo_editing photo-editing advice in masking exercises",
        requested,
    )


def test_goal_negation_hint_cannot_supersede_an_unrelated_preference(
    store: MemoryStore,
) -> None:
    service = NeoChatService(store.db, ollama=None)
    assert service.persist_user_memory(VIDEO_EDITING_INITIAL).saved_count == 2
    unrelated = (
        "Remember this: I prefer photo-editing advice in comparisons with long-form "
        "cinematic YouTube videos."
    )
    assert service.persist_user_memory(unrelated).saved_count == 1

    correction_result = service.persist_user_memory(VIDEO_EDITING_CORRECTION)

    assert (correction_result.saved_count, correction_result.updated_count) == (0, 2)
    active_preferences = {
        preference.canonical_slot: preference.value
        for preference in store.list_preferences()
    }
    assert active_preferences == {
        "preference:video_editing": (
            "video-editing advice in quick 15-minute drills with one hook, one cut, and "
            "one caption"
        ),
        "preference:photo_editing": (
            "photo-editing advice in comparisons with long-form cinematic YouTube videos"
        ),
    }
    photo_memory = next(
        memory
        for memory in store.list_memories(limit=100)
        if memory.canonical_slot == "preference:photo_editing"
    )
    assert photo_memory.is_active
    assert photo_memory.status == "active"


def test_domain_specific_advice_preference_is_never_global_response_style(
    store: MemoryStore,
) -> None:
    result = NeoChatService(store.db, ollama=None).persist_user_memory(PUBLIC_SPEAKING_INITIAL)

    assert result.saved_count == 2
    preference = store.list_preferences()[0]
    assert preference.category == "public_speaking"
    assert preference.canonical_slot == "preference:public_speaking"
    assert "response_style" not in preference.category


def test_domain_phrase_containing_in_general_is_not_a_global_response_style() -> None:
    extraction = MemoryExtractionService().extract(
        ExtractionRequest(
            text=(
                "Remember this: I prefer physics advice in general relativity examples "
                "with one equation and one diagram."
            )
        )
    )

    assert len(extraction.preferences) == 1
    preference = extraction.preferences[0]
    assert preference.attributes["category"] == "physics"
    assert preference.attributes["domain"] == "physics"
    assert preference.attributes["scope"] == "domain_specific"
    assert preference.attributes["canonical_slot"] == "preference:physics"
