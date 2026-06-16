from app.models.enums import CandidateStatus
from app.repositories.memory_store import MemoryStore
from app.services.context import ContextAssemblyService
from app.services.extraction import ConversationMessage, ExtractionRequest, MemoryExtractionService
from app.services.reflection import ReflectionRunRequest, ReflectionService
from app.services.retrieval import RetrievalRequest
from app.services.review import MemoryReviewRequest, MemoryReviewService


def test_extract_review_and_retrieve_context(db_session) -> None:
    store = MemoryStore(db_session)
    extraction = MemoryExtractionService().extract(
        ExtractionRequest(
            text=(
                "My name is Soham. "
                "I prefer detailed technical explanations. "
                "I want to build Neo."
            )
        )
    )

    candidates = MemoryExtractionService().persist_candidates(store, extraction)
    assert len(candidates) == 3

    reviewer = MemoryReviewService()
    for candidate in candidates:
        reviewer.review(
            store,
            MemoryReviewRequest(
                candidate_id=candidate.id,
                decision=CandidateStatus.ACCEPTED,
            ),
        )

    context = ContextAssemblyService().assemble(
        store,
        RetrievalRequest(query="Neo technical explanations", include_archives=False),
    )

    assert context.profile[0].key == "name"
    assert context.preferences[0].category == "response_style"
    assert context.goals[0].goal == "build Neo"
    assert context.relevant_memories


class FakeOllama:
    def chat(self, _messages, temperature=0.0) -> str:
        return """
        {
          "items": [
            {
              "type": "preference",
              "text": "response_style = concise answers",
              "confidence": 0.91,
              "importance": 8,
              "attributes": {
                "category": "response_style",
                "value": "concise answers"
              }
            }
          ]
        }
        """


def test_llm_extraction_auto_accepts_memory(db_session) -> None:
    store = MemoryStore(db_session)
    extractor = MemoryExtractionService()
    extraction = extractor.extract_with_llm(
        ExtractionRequest(
            messages=[
                ConversationMessage(
                    role="user",
                    content="Please keep answers concise from now on.",
                )
            ]
        ),
        FakeOllama(),
    )

    candidates = extractor.persist_and_accept(store, extraction)

    assert candidates[0].status == CandidateStatus.ACCEPTED
    assert store.list_candidates(CandidateStatus.PENDING) == []
    assert store.list_preferences()[0].value == "concise answers"
    assert store.list_memories()[0].memory_text == "response_style = concise answers"


def test_memory_records_can_be_edited_and_deleted(db_session) -> None:
    store = MemoryStore(db_session)
    extractor = MemoryExtractionService()
    extraction = extractor.extract(ExtractionRequest(text="I prefer detailed answers."))
    extractor.persist_and_accept(store, extraction)

    preference = store.list_preferences()[0]
    store.update_preference(preference.id, "response_style", "concise answers", 8)
    updated_preference = store.get_preference(preference.id)

    assert updated_preference is not None
    assert updated_preference.value == "concise answers"
    assert store.list_memories()[0].memory_text == "response_style = concise answers"

    store.delete_preference(preference.id)

    assert store.list_preferences() == []
    assert store.list_memories() == []


def test_conflicting_profile_fact_marks_old_fact_inactive(db_session) -> None:
    store = MemoryStore(db_session)
    extractor = MemoryExtractionService()
    reviewer = MemoryReviewService()

    first = extractor.extract(ExtractionRequest(text="I live in Mumbai."))
    first_candidates = extractor.persist_candidates(store, first)
    reviewer.review(
        store,
        MemoryReviewRequest(
            candidate_id=first_candidates[0].id,
            decision=CandidateStatus.ACCEPTED,
        ),
    )

    second = extractor.extract(ExtractionRequest(text="I live in Delhi."))
    second_candidates = extractor.persist_candidates(store, second)
    reviewer.review(
        store,
        MemoryReviewRequest(
            candidate_id=second_candidates[0].id,
            decision=CandidateStatus.ACCEPTED,
        ),
    )

    all_profile = store.list_profile(active_only=False)
    active_locations = [fact for fact in all_profile if fact.key == "location" and fact.is_active]

    assert len(active_locations) == 1
    assert active_locations[0].value == "Delhi"


def test_reflection_creates_reflection_and_candidate(db_session) -> None:
    store = MemoryStore(db_session)
    result = ReflectionService().run(store, ReflectionRunRequest(generate_candidates=True))

    assert result.reflection_id is not None
    assert result.candidate_ids

