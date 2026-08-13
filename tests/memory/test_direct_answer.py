"""Tier 5 — direct answers from canonical memory (plan section DAN).

When the user asks "what's my name", routing that through the chat model is both
wasteful and unreliable: the model has the answer in its context and may still
paraphrase it, hedge, or invent. This path answers such questions from stored
memory alone and returns a fixed sentence, or declines.

The interesting property is what it refuses to do. It answers only from records
recall actually selected, so it can say "I don't have that" — the one thing a
language model asked about your name will not reliably do.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.services.memory.contracts import Sensitivity
from app.services.memory.direct_answer import DirectMemoryAnswerService
from app.services.memory.queries import (
    CanonicalMemoryView,
    MemoryQueryContext,
    RecallDiagnostic,
    RecallItem,
    RecallMode,
    RecallPromptSelection,
    RecallQuery,
    RecallResult,
    RecallScoreBreakdown,
    SerializedMemoryContext,
)
from app.services.memory.taxonomy import MemoryType

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER = uuid4()


def _view(text: str, **overrides) -> CanonicalMemoryView:
    base = {
        "canonical_id": uuid4(),
        "owner_id": OWNER,
        "memory_type": MemoryType.IDENTITY,
        "domain_key": "global",
        "slot_key": "identity:global:name",
        "display_text": text,
        "sensitivity": Sensitivity.NORMAL,
        "confidence": 0.9,
        "importance": 5,
        "pinned": False,
        "usage_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
        "last_confirmed_at": NOW,
    }
    base.update(overrides)
    return CanonicalMemoryView(**base)


def _score() -> RecallScoreBreakdown:
    return RecallScoreBreakdown(
        domain_fit=1.0,
        lexical=1.0,
        importance=0.5,
        confidence=0.9,
        confirmation_freshness=1.0,
        recency=1.0,
        usage=0.0,
        pin=0.0,
        total=0.9,
    )


def _context(**overrides) -> MemoryQueryContext:
    base = {
        "owner_id": OWNER,
        "database_identity": "/tmp/memory.db",
        "profile_id": "profile-1",
        "request_id": "request-1",
        "current_time": NOW,
        "mode": RecallMode.SCOPED_LEXICAL,
    }
    base.update(overrides)
    return MemoryQueryContext(**base)


class StubOrchestrator:
    """Returns a scripted selection and records the query it was built from."""

    def __init__(self, *views: CanonicalMemoryView, serialize: bool = True) -> None:
        self.views = views
        self.serialize = serialize
        self.queries: list[RecallQuery] = []
        self.purposes: list[str] = []

    def build(self, query: RecallQuery, *, purpose: str) -> RecallPromptSelection:
        self.queries.append(query)
        self.purposes.append(purpose)
        recall = RecallResult(
            mode=query.context.mode,
            items=tuple(RecallItem(memory=view, score=_score()) for view in self.views),
            diagnostic=RecallDiagnostic(
                owner_database_binding="db",
                recall_mode=query.context.mode,
                eligible_candidate_count=len(self.views),
            ),
        )
        serialized = None
        if self.serialize and self.views:
            serialized = SerializedMemoryContext(
                content="block",
                canonical_ids=tuple(view.canonical_id for view in self.views),
                character_count=5,
            )
        return RecallPromptSelection(
            recall=recall,
            serialized=serialized,
            usage=None,
            usage_recorded=False,
        )


class TestQuestionRecognition:
    @pytest.mark.parametrize(
        "question",
        [
            "what's my name?",
            "what do you remember about me",
            "who am i?",
            "tell me my age",
            "how old am i",
            "remind me what i said",
        ],
    )
    def test_personal_memory_questions_are_recognised(self, question: str) -> None:
        """DAN-01a"""

        assert DirectMemoryAnswerService._is_personal_memory_question(question.casefold())

    @pytest.mark.parametrize(
        "question",
        [
            "what is the capital of france?",
            "what is python?",
            "explain recursion",
        ],
    )
    def test_general_questions_are_not(self, question: str) -> None:
        """DAN-01b — no first-person reference, so nothing personal is being asked."""

        assert not DirectMemoryAnswerService._is_personal_memory_question(question.casefold())

    def test_a_first_person_statement_is_not_a_question(self) -> None:
        """DAN-01c — the docstring's actual point.

        "my name is soham" mentions "my" and is first-person, but it is an
        assertion for the *extraction* path, not a question for the recall-only
        path. Answering it would reply "From your saved memory: …" to someone
        who was telling Neo something new.
        """

        assert not DirectMemoryAnswerService._is_personal_memory_question("my name is soham")


class TestMemoryTypeRouting:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("what's my name?", MemoryType.IDENTITY),
            ("how old am i?", MemoryType.IDENTITY),
            ("where do i work?", MemoryType.IDENTITY),
            ("what are my goals?", MemoryType.GOAL),
            ("what do i prefer?", MemoryType.PREFERENCE),
            ("what am i working on?", MemoryType.PROJECT),
            ("what activities do i do?", MemoryType.ACTIVITY),
        ],
    )
    def test_question_shapes_route_to_a_type(
        self, question: str, expected: MemoryType
    ) -> None:
        """DAN-02a"""

        assert DirectMemoryAnswerService._memory_type(question.casefold()) is expected

    @pytest.mark.parametrize(
        "question",
        ["what is the weather?", "how do i manage this?", "what should i do?"],
    )
    def test_an_unclear_question_routes_nowhere(self, question: str) -> None:
        """DAN-02b — including the `manage`/`age` regression the code comments name.

        Without the grouped alternation, "age" matched inside "manage" and sent
        an unrelated question to identity recall, which would answer a question
        about task management with the user's date of birth.
        """

        assert DirectMemoryAnswerService._memory_type(question.casefold()) is None


class TestTrustedSlots:
    @pytest.mark.parametrize(
        ("question", "expected"),
        [
            ("what's my name?", ("identity:global:name",)),
            ("how old am i?", ("identity:global:age",)),
            ("where am i from?", ("identity:global:origin",)),
            ("where do i live?", ("identity:global:current_location",)),
        ],
    )
    def test_an_identity_question_names_its_slot(
        self, question: str, expected: tuple[str, ...]
    ) -> None:
        """DAN-03 — deterministic recall needs a selector, and this supplies it."""

        assert DirectMemoryAnswerService._trusted_slots(question.casefold()) == expected

    def test_employment_questions_offer_both_plausible_slots(self) -> None:
        """"Where do I work" could mean the employer or the occupation."""

        assert DirectMemoryAnswerService._trusted_slots("where do i work?") == (
            "identity:global:employer",
            "identity:global:occupation",
        )

    def test_a_non_identity_question_names_no_slot(self) -> None:
        assert DirectMemoryAnswerService._trusted_slots("what are my goals?") == ()


class TestAnswering:
    def test_a_non_memory_question_falls_through(self) -> None:
        """DAN-04 — None means "not mine", and chat handles it."""

        service = DirectMemoryAnswerService(StubOrchestrator(_view("Soham")))

        assert service.answer("what is the capital of france?", context=_context()) is None

    def test_an_answer_is_returned_from_the_stored_record(self) -> None:
        """DAN-05"""

        service = DirectMemoryAnswerService(StubOrchestrator(_view("Soham")))

        answer = service.answer("what's my name?", context=_context())

        assert answer == "From your saved memory: Soham"

    def test_several_records_are_listed(self) -> None:
        orchestrator = StubOrchestrator(
            _view("EY GDS", slot_key="identity:global:employer"),
            _view("designer", slot_key="identity:global:occupation"),
        )
        service = DirectMemoryAnswerService(orchestrator)

        answer = service.answer("where do i work?", context=_context())

        assert answer is not None
        assert answer.startswith("From your saved memories:")
        assert "- EY GDS" in answer
        assert "- designer" in answer

    def test_a_missing_record_declines_rather_than_returning_none(self) -> None:
        """DAN-06 — corrected: it returns a refusal sentence, not None.

        The plan expected None here. Returning None would hand the question to
        the chat model, which has the conversation in context and will often
        produce a confident guess. The explicit refusal is the stronger
        behaviour: the one thing this path can do that a language model cannot is
        reliably say "I don't know".
        """

        service = DirectMemoryAnswerService(StubOrchestrator())

        answer = service.answer("what's my name?", context=_context())

        assert answer == "I do not have an active saved memory that answers that yet."

    def test_a_recognised_question_uses_deterministic_mode(self) -> None:
        """A scoped-lexical match could answer "what is my name" with anything."""

        orchestrator = StubOrchestrator(_view("Soham"))
        DirectMemoryAnswerService(orchestrator).answer("what's my name?", context=_context())

        assert orchestrator.queries[0].context.mode is RecallMode.DETERMINISTIC
        assert orchestrator.purposes == ["direct_answer"]

    def test_a_broad_question_uses_broad_mode(self) -> None:
        orchestrator = StubOrchestrator(_view("Soham"))
        DirectMemoryAnswerService(orchestrator).answer(
            "what do you remember about me", context=_context()
        )

        assert orchestrator.queries[0].context.mode is RecallMode.BROAD

    @pytest.mark.parametrize(
        "gate",
        [{"memory_enabled": False}, {"incognito": True}],
        ids=["disabled", "incognito"],
    )
    def test_a_gated_context_answers_nothing(self, gate: dict) -> None:
        """DAN-07 — and the orchestrator is not even consulted."""

        orchestrator = StubOrchestrator(_view("Soham"))
        service = DirectMemoryAnswerService(orchestrator)

        assert service.answer("what's my name?", context=_context(**gate)) is None
        assert orchestrator.queries == []

    def test_a_missing_context_answers_nothing(self) -> None:
        service = DirectMemoryAnswerService(StubOrchestrator(_view("Soham")))

        assert service.answer("what's my name?", context=None) is None

    def test_the_disabled_flag_turns_the_path_off(self) -> None:
        """DAN-10 — `direct_answer_reads_enabled` reaches this as `enabled`."""

        orchestrator = StubOrchestrator(_view("Soham"))
        service = DirectMemoryAnswerService(orchestrator, enabled=False)

        assert service.answer("what's my name?", context=_context()) is None
        assert orchestrator.queries == []

    def test_no_orchestrator_answers_nothing(self) -> None:
        assert DirectMemoryAnswerService(None).answer("what's my name?", context=_context()) is None

    def test_only_serialized_records_are_answered_from(self) -> None:
        """The answer must match what usage accounting recorded as shown.

        `answer` filters recall items down to the ids the serializer actually
        emitted. A record that recall selected but the budget dropped is not part
        of the answer — otherwise Neo would state a fact it never counted as used.
        """

        orchestrator = StubOrchestrator(_view("Soham"), serialize=False)
        service = DirectMemoryAnswerService(orchestrator)

        answer = service.answer("what's my name?", context=_context())

        assert answer == "I do not have an active saved memory that answers that yet."
        assert service.last_canonical_ids == ()

    def test_the_answered_ids_are_exposed_for_accounting(self) -> None:
        view = _view("Soham")
        service = DirectMemoryAnswerService(StubOrchestrator(view))

        service.answer("what's my name?", context=_context())

        assert service.last_canonical_ids == (view.canonical_id,)
        assert service.last_selection is not None

    def test_state_from_a_previous_answer_is_cleared(self) -> None:
        """Stale ids would attribute one turn's usage to the next."""

        service = DirectMemoryAnswerService(StubOrchestrator(_view("Soham")))
        service.answer("what's my name?", context=_context())

        service.answer("what is the capital of france?", context=_context())

        assert service.last_canonical_ids == ()
        assert service.last_selection is None


class TestSensitivityAndOwnershipAreUpstream:
    """DAN-08 / DAN-09 — corrected: neither is enforced here.

    `direct_answer.py` reads neither `sensitivity` nor `owner_id`; it answers
    from whatever the orchestrator returns. Sensitivity is filtered by recall
    (RCL-07/08) with QRY-02 preventing the unlocking flag outside deterministic
    mode, and owner scoping comes from the repository the recall service was
    built against.

    This is the fourth instance of the same shape as COR-23/24 and PMT-14, and
    the reason is structural rather than accidental: recall is the only component
    that touches the database, so every consumer downstream of it inherits its
    filtering instead of repeating it.
    """

    def test_a_sensitive_record_is_answered_if_recall_returned_it(self) -> None:
        service = DirectMemoryAnswerService(
            StubOrchestrator(_view("I have asthma", sensitivity=Sensitivity.SENSITIVE))
        )

        answer = service.answer("what's my name?", context=_context())

        assert answer == "From your saved memory: I have asthma"

    def test_the_context_owner_is_carried_into_the_query(self) -> None:
        """What this layer *does* guarantee: it never rewrites the owner.

        Owner scoping is inherited, but it can still be broken here by building a
        query against a different context. Asserting the owner survives the
        `model_copy` that switches the mode is the part this layer owns.
        """

        orchestrator = StubOrchestrator(_view("Soham"))
        DirectMemoryAnswerService(orchestrator).answer("what's my name?", context=_context())

        assert orchestrator.queries[0].context.owner_id == OWNER
