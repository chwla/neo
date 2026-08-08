from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.memory import (
    MemoryOperation,
    MemoryOutbox,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
)
from app.services.memory.contracts import (
    CandidateIntent,
    CreateMemoryCommand,
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOutcome,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    TargetRevision,
)
from tests.memory.helpers import OWNER_A, actor, candidate, source


def test_critical_video_goal_replacement_is_clean_atomic_and_replayable(
    mutation_service,
    phase2_engine,
) -> None:
    initial = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="critical-initial-goal",
        actor=actor(),
        source=source(),
        candidate=candidate("create long-form cinematic YouTube videos"),
    )
    created = mutation_service.execute(initial)
    predecessor_id = created.active_memory_ids[0]
    assert created.outcome is MemoryOutcome.CREATED

    replacement_candidate = candidate(
        "create short Instagram reels clearly",
        intent=CandidateIntent.REPLACE,
        targets=(predecessor_id,),
    )
    correction = ReplaceMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="critical-video-correction",
        actor=actor(),
        source=source(include_correction_evidence=True),
        candidate=replacement_candidate,
        authority=ReplacementAuthority.EXPLICIT_CORRECTION,
        targets=(TargetRevision(memory_id=predecessor_id, expected_revision=1),),
    )

    result = mutation_service.execute(correction)
    replay = mutation_service.execute(correction)

    assert result.outcome is MemoryOutcome.REPLACED
    assert replay == result
    replacement_id = result.active_memory_ids[0]
    with Session(phase2_engine) as session:
        active = session.scalars(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == OWNER_A,
                MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
            )
        ).all()
        assert len(active) == 1
        assert active[0].id == str(replacement_id)
        assert active[0].canonical_payload == "create short Instagram reels clearly"
        assert active[0].display_text == "create short Instagram reels clearly"
        assert active[0].domain_key == "video_creation"
        assert active[0].slot_key == "goal:video_creation:current_primary_goal"
        assert "no longer" not in active[0].canonical_payload.casefold()

        predecessor = session.get(MemoryRecord, str(predecessor_id))
        assert predecessor is not None
        assert predecessor.status == MemoryLifecycleState.SUPERSEDED.value
        relation = session.scalar(
            select(MemoryRelation).where(
                MemoryRelation.from_memory_id == str(replacement_id),
                MemoryRelation.to_memory_id == str(predecessor_id),
            )
        )
        assert relation is not None and relation.relation_type == "supersedes"
        roles = set(
            session.scalars(
                select(MemorySource.assertion_role).where(
                    MemorySource.operation_id == str(result.operation_id)
                )
            )
        )
        assert roles == {"supports", "retracts_predecessor"}
        event_kinds = list(
            session.scalars(
                select(MemoryOutbox.event_kind).where(
                    MemoryOutbox.event_idempotency_key.contains(str(result.operation_id))
                )
            )
        )
        assert sorted(event_kinds) == ["canonical_remove", "canonical_upsert"]
        event_keys = list(
            session.scalars(
                select(MemoryOutbox.event_idempotency_key).where(
                    MemoryOutbox.event_idempotency_key.contains(str(result.operation_id))
                )
            )
        )
        assert event_keys and all(OWNER_A in key for key in event_keys)
        assert session.scalar(select(func.count(MemoryOperation.id))) == 2
        assert session.scalar(select(func.count(MemoryRecord.id))) == 2
        assert session.scalar(select(func.count(MemoryRelation.id))) == 1


def test_same_idempotency_key_with_different_request_conflicts(
    mutation_service,
    phase2_engine,
) -> None:
    original = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="idempotency-conflict-key",
        actor=actor(),
        source=source(),
        candidate=candidate("create short Instagram reels clearly"),
    )
    first = mutation_service.execute(original)
    changed = original.model_copy(update={"candidate": candidate("create concise tutorial videos")})
    conflict = mutation_service.execute(changed)

    assert first.outcome is MemoryOutcome.CREATED
    assert conflict.outcome is MemoryOutcome.FAILED
    assert conflict.error_code is MemoryErrorCode.IDEMPOTENCY_CONFLICT
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecord.id))) == 1
        assert session.scalar(select(func.count(MemoryOperation.id))) == 1
