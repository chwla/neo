"""Regression tests for explicit forget and for restatement duplicating a memory.

Both failures were observed together in the running app: asking what was
remembered appended fresh copies of facts stated in earlier turns, and an
explicit "forget ..." then resolved to no single target and silently became a
review item, so the value came back on the next recall.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.services.memory.contracts import (
    CanonicalMemorySnapshot,
    MemoryLifecycleState,
    Sensitivity,
)
from app.services.memory.correction_resolver import (
    CorrectionResolutionKind,
    DeterministicCorrectionResolver,
    build_candidate,
)
from app.services.memory.extraction_contracts import ExtractionRequest, GroundingDecision
from app.services.memory.grounding import ground_assertion
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelRetractionProposal,
    ModelSourceSpan,
    SubjectHint,
)
from app.services.memory.taxonomy import Cardinality, MemoryType

OWNER = "11111111-1111-4111-8111-111111111111"


def _request(message: str, *, message_id: str) -> ExtractionRequest:
    from app.services.memory.extraction_contracts import ExtractionMode

    return ExtractionRequest(
        request_id=f"test:{message_id}",
        owner_id=OWNER,
        conversation_id="1",
        session_id="test",
        message_id=message_id,
        user_message=message,
        explicit_memory_intent=True,
        mode=ExtractionMode.FOREGROUND_DETERMINISTIC,
        source_content_hash=ExtractionRequest.content_hash(message),
    )


def _assertion(
    message: str,
    *,
    message_id: str,
    value: str,
    display: str | None = None,
    memory_type: MemoryType = MemoryType.GOAL,
) -> tuple[ExtractionRequest, ModelAssertionProposal, GroundingDecision]:
    request = _request(message, message_id=message_id)
    quoted = display or value
    start = message.index(quoted)
    span = ModelSourceSpan(
        message_id=message_id,
        start=start,
        end=start + len(quoted),
        quoted_text=quoted,
    )
    proposal = ModelAssertionProposal(
        proposal_id=f"assert-{message_id}",
        source_spans=(span,),
        subject_hint=SubjectHint.USER,
        memory_type_hint=memory_type,
        typed_value=value,
        display_hint=display or value,
        durability=DurabilityHint.DURABLE,
        confidence=0.9,
        sensitivity_hint=Sensitivity.NORMAL,
    )
    grounding = ground_assertion(request, proposal)
    assert grounding.accepted, grounding.reason
    return request, proposal, grounding


def _slot_for_value(
    value: str,
    *,
    message_id: str,
    display: str | None = None,
    memory_type: MemoryType = MemoryType.GOAL,
) -> str:
    message = f"I want to {display or value} always"
    request, proposal, grounding = _assertion(
        message,
        message_id=message_id,
        value=value,
        display=display or value,
        memory_type=memory_type,
    )
    result = build_candidate(request, proposal, grounding, ())
    assert result.candidate is not None, result.reason
    return result.candidate.proposal.slot_key


def _snapshot(
    value: str,
    *,
    slot_key: str,
    memory_type: MemoryType = MemoryType.GOAL,
    domain: str = "global",
) -> CanonicalMemorySnapshot:
    return CanonicalMemorySnapshot(
        memory_id=uuid4(),
        owner_id=OWNER,
        subject_key="user",
        memory_type=memory_type,
        domain_key=domain,
        slot_key=slot_key,
        cardinality=Cardinality.ADDITIVE,
        canonical_value=value,
        display_text=value,
        sensitivity=Sensitivity.NORMAL,
        status=MemoryLifecycleState.ACTIVE,
        revision=1,
    )


def _retraction(message: str, *, hint: str) -> ModelRetractionProposal:
    start = message.index(hint)
    return ModelRetractionProposal(
        proposal_id="retract-1",
        source_spans=(
            ModelSourceSpan(message_id="9", start=start, end=start + len(hint), quoted_text=hint),
        ),
        subject_hint=SubjectHint.USER,
        old_value_hint=hint,
        confidence=0.9,
        explicit_forget=True,
    )


class TestStableSlots:
    def test_same_value_restated_in_a_later_message_reuses_its_slot(self) -> None:
        """The duplicate bug: a later turn re-extracting an earlier fact."""

        first = _slot_for_value("improve at urban sketching", message_id="1")
        second = _slot_for_value("improve at urban sketching", message_id="7")
        assert first == second

    def test_slug_and_prose_forms_of_one_value_share_a_slot(self) -> None:
        """The model alternates between prose and a slug for the same fact."""

        prose = _slot_for_value("improve at urban sketching", message_id="1")
        slug = _slot_for_value(
            "improve_at_urban_sketching",
            message_id="7",
            display="improve at urban sketching",
        )
        assert prose == slug

    def test_case_variants_of_one_value_share_a_slot(self) -> None:
        lower = _slot_for_value("improve at urban sketching", message_id="1")
        upper = _slot_for_value("Improve at urban sketching", message_id="7")
        assert lower == upper

    def test_distinct_values_keep_distinct_slots(self) -> None:
        sketching = _slot_for_value("improve at urban sketching", message_id="1")
        running = _slot_for_value("improve my 5K running", message_id="2")
        assert sketching != running

    def test_same_value_under_a_different_slot_reconfirms(self) -> None:
        """The model names a different preference dimension for one preference."""

        slot = _slot_for_value("improve at urban sketching", message_id="1")
        existing = _snapshot(
            "improve at urban sketching",
            slot_key=slot.replace("independent", "renamed"),
        )

        message = "I want to improve at urban sketching always"
        request, proposal, grounding = _assertion(
            message, message_id="7", value="improve at urban sketching"
        )
        built = build_candidate(request, proposal, grounding, ())
        assert built.candidate is not None

        resolution = DeterministicCorrectionResolver().resolve(built.candidate, (existing,))
        assert resolution.kind is CorrectionResolutionKind.RECONFIRM
        assert resolution.targets == (existing,)

    def test_a_different_value_still_creates_a_record(self) -> None:
        existing = _snapshot(
            "improve at urban sketching",
            slot_key=_slot_for_value("improve at urban sketching", message_id="1"),
        )

        message = "I want to improve my 5K running always"
        request, proposal, grounding = _assertion(
            message, message_id="7", value="improve my 5K running"
        )
        built = build_candidate(request, proposal, grounding, ())
        assert built.candidate is not None

        resolution = DeterministicCorrectionResolver().resolve(built.candidate, (existing,))
        assert resolution.kind is CorrectionResolutionKind.CREATE

    def test_restated_fact_resolves_as_a_reconfirmation_not_a_new_record(self) -> None:
        slot = _slot_for_value("improve at urban sketching", message_id="1")
        existing = _snapshot("improve at urban sketching", slot_key=slot)

        message = "I want to improve at urban sketching always"
        request, proposal, grounding = _assertion(
            message, message_id="7", value="improve at urban sketching"
        )
        built = build_candidate(request, proposal, grounding, ())
        assert built.candidate is not None

        resolution = DeterministicCorrectionResolver().resolve(built.candidate, (existing,))
        assert resolution.kind is CorrectionResolutionKind.RECONFIRM


class TestExplicitForget:
    def test_forget_matches_a_value_named_inside_a_sentence(self) -> None:
        """The delete bug: the user names the value in prose, not verbatim."""

        message = "Forget that I use a fineliner pen and a pocket watercolor set for sketching."
        hint = "that I use a fineliner pen and a pocket watercolor set for sketching"
        stored = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (stored,)
        )
        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert resolution.targets == (stored,)

    def test_forget_removes_every_duplicate_of_the_same_value(self) -> None:
        """Retracting only the first copy let the value return on next recall."""

        message = "Forget that I use a fineliner pen and a pocket watercolor set."
        hint = "that I use a fineliner pen and a pocket watercolor set"
        first = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )
        second = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:bbbb",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (first, second)
        )
        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert set(resolution.targets) == {first, second}

    def test_exact_value_still_retracts(self) -> None:
        message = "Forget fineliner pen and a pocket watercolor set"
        hint = "fineliner pen and a pocket watercolor set"
        stored = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (stored,)
        )
        assert resolution.kind is CorrectionResolutionKind.RETRACT

    def test_naming_one_word_of_a_remembered_phrase_does_not_delete_it(self) -> None:
        """Containment runs one way only, so a partial name cannot over-delete."""

        message = "Forget pen"
        stored = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint="pen"), (stored,)
        )
        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW
        assert resolution.targets == ()

    def test_unrelated_memory_is_never_retracted(self) -> None:
        message = "Forget that I use a fineliner pen and a pocket watercolor set."
        hint = "that I use a fineliner pen and a pocket watercolor set"
        unrelated = _snapshot("improve at urban sketching", slot_key="goal:global:independent:cccc")

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (unrelated,)
        )
        assert resolution.kind is CorrectionResolutionKind.NEEDS_REVIEW

    def test_forget_naming_two_stored_facts_removes_both(self) -> None:
        """The model often stores "A and B" as two atomic records."""

        message = "Forget a fineliner pen and a pocket watercolor set"
        hint = "a fineliner pen and a pocket watercolor set"
        pen = _snapshot(
            "fineliner pen",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )
        paints = _snapshot(
            "pocket watercolor set",
            slot_key="knowledge:global:item:bbbb",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (pen, paints)
        )
        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert set(resolution.targets) == {pen, paints}

    def test_a_fragment_of_a_longer_match_is_left_alone(self) -> None:
        """Only what the user named goes; a substring of it is not a target."""

        message = "Forget a fineliner pen and a pocket watercolor set"
        hint = "a fineliner pen and a pocket watercolor set"
        whole = _snapshot(
            "fineliner pen and a pocket watercolor set",
            slot_key="knowledge:global:item:aaaa",
            memory_type=MemoryType.KNOWLEDGE,
        )
        fragment = _snapshot(
            "pen",
            slot_key="knowledge:global:item:bbbb",
            memory_type=MemoryType.KNOWLEDGE,
        )

        resolution = DeterministicCorrectionResolver().resolve_retraction(
            _retraction(message, hint=hint), (whole, fragment)
        )
        assert resolution.kind is CorrectionResolutionKind.RETRACT
        assert resolution.targets == (whole,)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))


class TestDisplayTextGrounding:
    """Stored display text must be words the user actually wrote."""

    def test_an_ungrounded_display_hint_falls_back_to_the_value(self) -> None:
        message = "I prefer simple 25-minute practice sessions with shading notes always"
        value = "simple 25-minute practice sessions with shading notes"
        request = _request(message, message_id="1")
        start = message.index(value)
        proposal = ModelAssertionProposal(
            proposal_id="assert-1",
            source_spans=(
                ModelSourceSpan(
                    message_id="1", start=start, end=start + len(value), quoted_text=value
                ),
            ),
            subject_hint=SubjectHint.USER,
            memory_type_hint=MemoryType.PREFERENCE,
            slot_hint="practice_format",
            typed_value=value,
            # The model summarises in words the user never wrote.
            display_hint="preference for structured sketching practice sessions",
            durability=DurabilityHint.DURABLE,
            confidence=0.9,
            sensitivity_hint=Sensitivity.NORMAL,
        )
        grounding = ground_assertion(request, proposal)
        assert grounding.accepted, grounding.reason

        built = build_candidate(request, proposal, grounding, ())
        assert built.candidate is not None
        assert built.candidate.proposal.display_text == value
        assert "structured" not in built.candidate.proposal.display_text

    def test_a_grounded_display_hint_is_kept(self) -> None:
        message = "I prefer simple 25-minute practice sessions with shading notes always"
        value = "simple 25-minute practice sessions with shading notes"
        request, proposal, grounding = _assertion(
            message,
            message_id="1",
            value=value,
            display=value,
            memory_type=MemoryType.PREFERENCE,
        )
        built = build_candidate(request, proposal, grounding, ())
        assert built.candidate is not None
        assert built.candidate.proposal.display_text == value
