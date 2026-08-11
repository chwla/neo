"""Tests for the embedding check that catches a fact restated in other words.

String comparison cannot tell that "simple 25-minute sessions with perspective
steps" and "simple 25-minute practice sessions with perspective steps" are one
preference, so both were stored and a later delete had two targets.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from app.services.memory.contracts import (
    CandidateGroundingSpan,
    CandidateIntent,
    CandidateTargetHints,
    CanonicalMemorySnapshot,
    EvidenceRole,
    EvidenceSpan,
    MemoryLifecycleState,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory.extraction_coordinator import MemoryExtractionCoordinator
from app.services.memory.extraction_contracts import NormalizedExtractionCandidate
from app.services.memory.taxonomy import Cardinality, MemoryType

OWNER = "11111111-1111-4111-8111-111111111111"


def _snapshot(
    value: str,
    *,
    memory_type: MemoryType = MemoryType.PREFERENCE,
    domain: str = "global",
    slot: str = "preference:global:practice_session_format",
) -> CanonicalMemorySnapshot:
    return CanonicalMemorySnapshot(
        memory_id=uuid4(),
        owner_id=OWNER,
        subject_key="user",
        memory_type=memory_type,
        domain_key=domain,
        slot_key=slot,
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=value,
        display_text=value,
        sensitivity=Sensitivity.NORMAL,
        status=MemoryLifecycleState.ACTIVE,
        revision=1,
    )


def _candidate(
    value: str,
    *,
    memory_type: MemoryType = MemoryType.PREFERENCE,
    domain: str = "global",
    slot: str = "preference:global:urban_sketching_practice",
) -> NormalizedExtractionCandidate:
    proposal = ValidatedCandidateProposal(
        proposal_id=uuid4(),
        intent=CandidateIntent.ASSERT,
        subject_key="user",
        memory_type=memory_type,
        domain_key=domain,
        slot_key=slot,
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=value,
        display_text=value,
        sensitivity=Sensitivity.NORMAL,
        confidence=0.9,
        importance=7,
        explicit_user_request=True,
        target_hints=CandidateTargetHints(),
        evidence=(
            EvidenceSpan(role=EvidenceRole.ASSERTION, text=value, start=0, end=len(value)),
        ),
    )
    span = CandidateGroundingSpan(
        message_id="1",
        role=EvidenceRole.ASSERTION,
        start=0,
        end=len(value),
        content_hash=hashlib.sha256(value.encode()).hexdigest(),
    )
    return NormalizedExtractionCandidate(
        proposal=proposal,
        grounding_spans=(span,),
        correction_group=None,
        old_value_hints=(),
    )


def _coordinator(finder, *, threshold: float = 0.93) -> MemoryExtractionCoordinator:
    return MemoryExtractionCoordinator(
        adapter=None,
        duplicate_finder=finder,
        duplicate_threshold=threshold,
    )


class TestSemanticDuplicate:
    def test_a_restatement_in_other_words_is_found(self) -> None:
        existing = _snapshot(
            "simple 25-minute practice sessions with perspective steps and shading notes"
        )

        def finder(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
            assert existing.memory_id in allowed
            return existing.memory_id if 0.97 >= threshold else None

        candidate = _candidate(
            "simple 25-minute sessions with perspective steps and shading notes"
        )
        found = _coordinator(finder)._semantic_duplicate(candidate, (existing,))
        assert found == existing

    def test_a_score_below_the_threshold_is_not_a_duplicate(self) -> None:
        existing = _snapshot("simple 25-minute practice sessions")

        def finder(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
            return None

        candidate = _candidate("I want to improve at urban sketching")
        assert _coordinator(finder)._semantic_duplicate(candidate, (existing,)) is None

    def test_a_different_memory_type_is_never_compared(self) -> None:
        """A near-miss must not reach across categories."""

        existing = _snapshot("improve at urban sketching", memory_type=MemoryType.GOAL)
        calls: list[frozenset[UUID]] = []

        def finder(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
            calls.append(allowed)
            return next(iter(allowed), None)

        candidate = _candidate("improve at urban sketching", memory_type=MemoryType.PREFERENCE)
        assert _coordinator(finder)._semantic_duplicate(candidate, (existing,)) is None
        assert calls == []

    def test_a_different_domain_is_never_compared(self) -> None:
        existing = _snapshot("25-minute sessions", domain="topic.urban_sketching")

        def finder(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
            return next(iter(allowed), None)

        candidate = _candidate("25-minute sessions", domain="global")
        assert _coordinator(finder)._semantic_duplicate(candidate, (existing,)) is None

    def test_no_finder_configured_never_blocks_a_write(self) -> None:
        existing = _snapshot("simple 25-minute practice sessions")
        candidate = _candidate("simple 25-minute sessions")
        assert _coordinator(None)._semantic_duplicate(candidate, (existing,)) is None

    def test_a_failing_provider_never_blocks_a_write(self) -> None:
        existing = _snapshot("simple 25-minute practice sessions")

        def finder(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
            raise RuntimeError("embedding provider down")

        candidate = _candidate("simple 25-minute sessions")
        assert _coordinator(finder)._semantic_duplicate(candidate, (existing,)) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
