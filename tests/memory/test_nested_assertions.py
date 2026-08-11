"""Tests for dropping an assertion that is a fragment of another assertion.

Asked to record one preference sentence, the model proposed both the whole
sentence and "perspective steps" cut out of its middle, which stored a spurious
second preference and put an exclusive-slot conflict into review.
"""

from __future__ import annotations

import pytest

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
from app.services.memory.model_schema import (
    DurabilityHint,
    ModelAssertionProposal,
    ModelProposalResponse,
    ModelSourceSpan,
    SubjectHint,
)
from app.services.memory.taxonomy import MemoryType

MESSAGE = (
    "I prefer simple 25-minute practice sessions with perspective steps, "
    "line-control drills, shading notes, and progress tracking."
)


def _assertion(
    proposal_id: str,
    quoted: str,
    *,
    memory_type: MemoryType = MemoryType.PREFERENCE,
    message: str = MESSAGE,
) -> ModelAssertionProposal:
    start = message.index(quoted)
    return ModelAssertionProposal(
        proposal_id=proposal_id,
        source_spans=(
            ModelSourceSpan(
                message_id="1", start=start, end=start + len(quoted), quoted_text=quoted
            ),
        ),
        subject_hint=SubjectHint.USER,
        memory_type_hint=memory_type,
        typed_value=quoted,
        display_hint=quoted,
        durability=DurabilityHint.DURABLE,
        confidence=0.9,
        sensitivity_hint=Sensitivity.NORMAL,
    )


def _drop(*assertions: ModelAssertionProposal):
    return MemoryExtractionCoordinator._drop_nested_assertions(
        ModelProposalResponse(assertions=assertions)
    )


class TestNestedAssertions:
    def test_a_fragment_of_a_larger_assertion_is_dropped(self) -> None:
        whole = _assertion("a", "simple 25-minute practice sessions with perspective steps")
        fragment = _assertion("b", "perspective steps")

        response, dropped = _drop(whole, fragment)
        assert dropped == 1
        assert [item.proposal_id for item in response.assertions] == ["a"]

    def test_separate_facts_in_one_message_both_survive(self) -> None:
        message = "I want to improve at urban sketching. I prefer 25-minute sessions."
        goal = _assertion("a", "improve at urban sketching", message=message)
        preference = _assertion("b", "25-minute sessions", message=message)

        response, dropped = _drop(goal, preference)
        assert dropped == 0
        assert len(response.assertions) == 2

    def test_a_fragment_of_a_different_type_survives(self) -> None:
        """A goal quoted inside a wide preference span is not part of it."""

        whole = _assertion("a", "simple 25-minute practice sessions with perspective steps")
        goal = _assertion("b", "perspective steps", memory_type=MemoryType.GOAL)

        response, dropped = _drop(whole, goal)
        assert dropped == 0
        assert len(response.assertions) == 2

    def test_identical_spans_both_survive(self) -> None:
        first = _assertion("a", "perspective steps")
        second = _assertion("b", "perspective steps")

        response, dropped = _drop(first, second)
        assert dropped == 0

    def test_a_single_assertion_is_untouched(self) -> None:
        response, dropped = _drop(_assertion("a", "perspective steps"))
        assert dropped == 0
        assert len(response.assertions) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
