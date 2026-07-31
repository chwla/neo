from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.memory_v2 import MemoryCandidateV2, MemoryRecordV2, MemorySourceV2
from app.services.memory_v2.contracts import (
    CreateMemoryCommand,
    MemoryErrorCode,
    MemoryOutcome,
    MemoryRejectionCode,
    MemoryUpdatePatch,
    TargetRevision,
    UpdateMemoryCommand,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from tests.memory_v2.helpers import OWNER_A, actor, candidate, source


def _create(service, value: object, *, key: str, **candidate_kwargs):
    return service.execute(
        CreateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key=key,
            actor=actor(),
            source=source(),
            candidate=candidate(value, **candidate_kwargs),
        )
    )


def test_exact_typed_duplicate_reconfirms_without_new_record(
    mutation_service,
    phase2_engine,
) -> None:
    first = _create(
        mutation_service,
        {"format": "concise", "examples": True},
        key="typed-duplicate-first",
        display="Use concise answers with examples",
        memory_type=MemoryType.PREFERENCE,
        domain="software_development",
        slot="preference:software_development:answer_format",
    )
    duplicate = _create(
        mutation_service,
        {"examples": True, "format": "concise"},
        key="typed-duplicate-second",
        display="Examples with concise answers",
        memory_type=MemoryType.PREFERENCE,
        domain="software_development",
        slot="preference:software_development:answer_format",
    )

    assert first.outcome is MemoryOutcome.CREATED
    assert duplicate.outcome is MemoryOutcome.RECONFIRMED
    assert duplicate.active_memory_ids == first.active_memory_ids
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 1
        record = session.get(MemoryRecordV2, str(first.active_memory_ids[0]))
        assert record is not None and record.revision == 2
        assert session.scalar(select(func.count(MemorySourceV2.id))) == 2


def test_similar_but_different_value_in_exclusive_slot_needs_review(
    mutation_service,
    phase2_engine,
) -> None:
    first = _create(
        mutation_service,
        "create short tutorial videos",
        key="similar-first",
    )
    conflict = _create(
        mutation_service,
        "create short cinematic videos",
        key="similar-second",
    )

    assert conflict.outcome is MemoryOutcome.NEEDS_REVIEW
    assert conflict.rejection_code is MemoryRejectionCode.AMBIGUOUS_CONFLICT
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 1
        active = session.get(MemoryRecordV2, str(first.active_memory_ids[0]))
        assert active is not None and active.canonical_payload == "create short tutorial videos"
        review = session.scalar(
            select(MemoryCandidateV2).where(MemoryCandidateV2.state == "needs_review")
        )
        assert review is not None


def test_compatible_refinement_updates_revision_and_incompatible_update_rejects(
    mutation_service,
    phase2_engine,
) -> None:
    created = _create(
        mutation_service,
        "create tutorial videos",
        key="refinement-create",
    )
    memory_id = created.active_memory_ids[0]
    refined = mutation_service.execute(
        UpdateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="compatible-refinement",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
            patch=MemoryUpdatePatch(
                canonical_value="create tutorial videos with practical examples",
                display_text="create tutorial videos with practical examples",
            ),
        )
    )
    incompatible = mutation_service.execute(
        UpdateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="incompatible-update",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=2),
            patch=MemoryUpdatePatch(
                canonical_value="build a travel planning business",
                display_text="build a travel planning business",
            ),
        )
    )

    assert refined.outcome is MemoryOutcome.REFINED
    assert refined.current_revision == 2
    assert incompatible.outcome is MemoryOutcome.REJECTED
    assert incompatible.rejection_code is MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE
    with Session(phase2_engine) as session:
        record = session.get(MemoryRecordV2, str(memory_id))
        assert record is not None
        assert record.revision == 2
        assert record.canonical_payload == "create tutorial videos with practical examples"


def test_additive_goals_are_independent_and_not_only_is_positive(
    mutation_service,
    phase2_engine,
) -> None:
    first_slot = f"goal:learning:independent:{uuid4()}"
    second_slot = f"goal:learning:independent:{uuid4()}"
    first = _create(
        mutation_service,
        "not only study mathematics but also practice it",
        key="additive-one",
        domain="learning",
        slot=first_slot,
        cardinality=Cardinality.ADDITIVE,
    )
    second = _create(
        mutation_service,
        "learn conversational Spanish",
        key="additive-two",
        domain="learning",
        slot=second_slot,
        cardinality=Cardinality.ADDITIVE,
    )
    assert first.outcome is MemoryOutcome.CREATED
    assert second.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 2


def test_grounded_unknown_domain_is_accepted_but_ungrounded_and_last_token_fail(
    mutation_service,
    phase2_engine,
) -> None:
    entity_id = uuid4()
    grounded = _create(
        mutation_service,
        "learn quantum computing foundations",
        key="grounded-domain",
        memory_type=MemoryType.KNOWLEDGE,
        domain="topic.quantum_computing",
        slot=f"knowledge:topic.quantum_computing:item:{entity_id}",
        cardinality=Cardinality.ADDITIVE,
    )
    ungrounded = _create(
        mutation_service,
        "learn advanced foundations",
        key="ungrounded-domain",
        memory_type=MemoryType.KNOWLEDGE,
        domain="topic.quantum_computing",
        slot=f"knowledge:topic.quantum_computing:item:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
    )
    last_token = _create(
        mutation_service,
        "create short Instagram reels clearly",
        key="last-token-domain",
        domain="clearly",
        slot="goal:clearly:current_primary_goal",
    )

    assert grounded.outcome is MemoryOutcome.CREATED
    assert ungrounded.outcome is MemoryOutcome.FAILED
    assert ungrounded.error_code is MemoryErrorCode.INVALID_COMMAND
    assert last_token.outcome is MemoryOutcome.FAILED
    assert last_token.error_code is MemoryErrorCode.INVALID_COMMAND
    with Session(phase2_engine) as session:
        records = session.scalars(select(MemoryRecordV2)).all()
        assert len(records) == 1
        assert records[0].domain_key == "topic.quantum_computing"
        assert "clearly" in records[0].canonical_payload or records[0].domain_key != "clearly"


def test_stale_revision_is_stable_failure(mutation_service) -> None:
    created = _create(mutation_service, "create tutorial videos", key="stale-create")
    stale = mutation_service.execute(
        UpdateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="stale-update",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=created.active_memory_ids[0], expected_revision=9),
            patch=MemoryUpdatePatch(importance=8),
        )
    )
    assert stale.outcome is MemoryOutcome.FAILED
    assert stale.error_code is MemoryErrorCode.REVISION_CONFLICT
