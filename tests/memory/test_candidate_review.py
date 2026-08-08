from __future__ import annotations

import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

from app.services.memory.adapters import (
    CandidateReviewAction,
    CandidateReviewAdapter,
    GenericMemoryAdapter,
    StructuredMemoryInput,
)
from app.services.memory.contracts import MemoryUpdatePatch, TargetRevision
from app.services.memory.idempotency import MemoryIdempotency
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.mutation_helpers import OWNER_A, memory_harness, video_goal


def _active_rows(path) -> list[tuple[str, str, str, str, int]]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT memory_type, domain_key, slot_key, canonical_payload, revision "
            "FROM memory_records WHERE status = 'active' ORDER BY memory_type, slot_key"
        ).fetchall()
    finally:
        connection.close()


def test_review_accept_duplicate_and_compatible_refinement_match_direct_commands(tmp_path) -> None:
    harness = memory_harness(tmp_path)
    review = CandidateReviewAdapter(harness.coordinator)
    item = video_goal("create tutorial videos")
    accepted = review.apply(
        harness.context,
        candidate_id="candidate-create",
        candidate_revision=1,
        action=CandidateReviewAction.ACCEPT,
        item=item,
    )
    duplicate = review.apply(
        replace(harness.context, source_id="phase3-source-duplicate"),
        candidate_id="candidate-duplicate",
        candidate_revision=1,
        action=CandidateReviewAction.ACCEPT,
        item=item,
    )
    assert accepted.outcome == "created"
    assert duplicate.outcome == "reconfirmed"
    assert accepted.coordination and accepted.coordination.mutation
    target = TargetRevision(
        memory_id=UUID(str(accepted.coordination.mutation.active_memory_ids[-1])),
        expected_revision=2,
    )
    refined = review.apply(
        harness.context,
        candidate_id="candidate-refinement",
        candidate_revision=1,
        action=CandidateReviewAction.REFINE,
        targets=(target,),
        patch=MemoryUpdatePatch(
            canonical_value="create tutorial videos with practical examples",
            display_text="create tutorial videos with practical examples",
        ),
    )
    assert refined.outcome == "refined"
    assert _active_rows(harness.database_path)[0][3:] == (
        '"create tutorial videos with practical examples"',
        3,
    )


def test_review_category_correction_is_explicit_typed_replacement(tmp_path) -> None:
    harness = memory_harness(tmp_path)
    generic = GenericMemoryAdapter(harness.coordinator)
    created = generic.create(
        harness.context,
        video_goal("give code advice as checklists"),
        idempotency_key=MemoryIdempotency.manual(OWNER_A, "mistaken-category"),
    )
    assert created.mutation and str(created.mutation.active_memory_ids[-1])
    corrected_item = StructuredMemoryInput(
        memory_type=MemoryType.PREFERENCE,
        domain_key="software_development",
        slot_key="preference:software_development:advice_format",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value="give code advice as checklists",
        display_text="give code advice as checklists",
    )
    corrected = CandidateReviewAdapter(harness.coordinator).apply(
        harness.context,
        candidate_id="candidate-category-correction",
        candidate_revision=1,
        action=CandidateReviewAction.CATEGORY_CORRECTION,
        item=corrected_item,
        targets=(
            TargetRevision(
                memory_id=UUID(str(created.mutation.active_memory_ids[-1])),
                expected_revision=1,
            ),
        ),
        explicit_domain_change=True,
        explicit_slot_change=True,
    )
    assert corrected.outcome == "replaced"
    assert _active_rows(harness.database_path) == [
        (
            "preference",
            "software_development",
            "preference:software_development:advice_format",
            '"give code advice as checklists"',
            1,
        )
    ]


def test_review_explicit_merge_uses_supplied_positive_value(tmp_path) -> None:
    harness = memory_harness(tmp_path)
    generic = GenericMemoryAdapter(harness.coordinator)

    def learning_goal(value: str) -> StructuredMemoryInput:
        return StructuredMemoryInput(
            memory_type=MemoryType.GOAL,
            domain_key="learning",
            slot_key=f"goal:learning:independent:{uuid4()}",
            cardinality=Cardinality.ADDITIVE,
            canonical_value=value,
            display_text=value,
        )

    first = generic.create(
        harness.context,
        learning_goal("study Python"),
        idempotency_key=MemoryIdempotency.manual(OWNER_A, "merge-first"),
    )
    second = generic.create(
        harness.context,
        learning_goal("practice Python projects"),
        idempotency_key=MemoryIdempotency.manual(OWNER_A, "merge-second"),
    )
    assert first.mutation and str(first.mutation.active_memory_ids[-1])
    assert second.mutation and str(second.mutation.active_memory_ids[-1])
    merged_value = "learn Python through study and practical projects"
    merged = CandidateReviewAdapter(harness.coordinator).apply(
        harness.context,
        candidate_id="candidate-merge",
        candidate_revision=1,
        action=CandidateReviewAction.MERGE,
        item=learning_goal(merged_value),
        targets=(
            TargetRevision(
                memory_id=UUID(str(first.mutation.active_memory_ids[-1])),
                expected_revision=1,
            ),
            TargetRevision(
                memory_id=UUID(str(second.mutation.active_memory_ids[-1])), expected_revision=1
            ),
        ),
    )
    assert merged.outcome == "merged"
    rows = _active_rows(harness.database_path)
    assert len(rows) == 1
    assert rows[0][3] == f'"{merged_value}"'
