from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
)
from app.services.memory_v2.contracts import (
    CandidateIntent,
    CreateMemoryCommand,
    ForgetMemoryCommand,
    MemoryErrorCode,
    MemoryOutcome,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    TargetRevision,
)
from app.services.memory_v2.mutations import MemoryMutationService, RetryPolicy
from tests.memory_v2.helpers import DATABASE_IDENTITY, OWNER_A, actor, candidate, source


def _service(engine, crypto, failure_injector=None):
    return MemoryMutationService(
        engine,
        owner_id=OWNER_A,
        database_identity=DATABASE_IDENTITY,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
        retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0),
        failure_injector=failure_injector,
    )


def _initial(service):
    return service.execute(
        CreateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="atomic-initial",
            actor=actor(),
            source=source(),
            candidate=candidate("create long-form cinematic YouTube videos"),
        )
    )


@pytest.mark.parametrize(
    "stage",
    [
        "operation_start",
        "first_predecessor_transition",
        "replacement_record_creation",
        "relation_creation",
        "provenance_creation",
        "outbox_creation",
        "operation_completion",
    ],
)
def test_replacement_failure_at_every_sql_stage_rolls_back_everything(
    phase2_engine,
    test_crypto,
    stage,
) -> None:
    initial_service = _service(phase2_engine, test_crypto)
    original = _initial(initial_service)

    def inject(current: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic-stage-failure")

    failing = _service(phase2_engine, test_crypto, inject)
    command = ReplaceMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key=f"atomic-replacement-{stage}",
        actor=actor(),
        source=source(include_correction_evidence=True),
        candidate=candidate(
            "create short Instagram reels clearly",
            intent=CandidateIntent.REPLACE,
            targets=(original.active_memory_ids[0],),
        ),
        authority=ReplacementAuthority.EXPLICIT_CORRECTION,
        targets=(TargetRevision(memory_id=original.active_memory_ids[0], expected_revision=1),),
    )
    result = failing.execute(command)

    assert result.outcome is MemoryOutcome.FAILED
    assert result.error_code is MemoryErrorCode.INTERNAL_ERROR
    assert result.message == "injected_mutation_failure"
    with Session(phase2_engine) as session:
        records = session.scalars(select(MemoryRecordV2)).all()
        assert len(records) == 1
        assert records[0].id == str(original.active_memory_ids[0])
        assert records[0].status == "active" and records[0].revision == 1
        assert session.scalar(select(func.count(MemoryOperationV2.id))) == 1
        assert session.scalar(select(func.count(MemoryCandidateV2.id))) == 1
        assert session.scalar(select(func.count(MemoryRelationV2.id))) == 0
        assert session.scalar(select(func.count(MemorySourceV2.id))) == 1
        assert session.scalar(select(func.count(MemoryOutboxV2.id))) == 1


def test_tombstone_stage_failure_rolls_back_forget(
    phase2_engine,
    test_crypto,
) -> None:
    initial_service = _service(phase2_engine, test_crypto)
    original = _initial(initial_service)

    def inject(current: str) -> None:
        if current == "tombstone_creation":
            raise RuntimeError("synthetic-stage-failure")

    failing = _service(phase2_engine, test_crypto, inject)
    result = failing.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="atomic-forget",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=original.active_memory_ids[0], expected_revision=1),
        )
    )

    assert result.outcome is MemoryOutcome.FAILED
    with Session(phase2_engine) as session:
        record = session.get(MemoryRecordV2, str(original.active_memory_ids[0]))
        assert record is not None
        assert record.status == "active" and record.revision == 1
        assert record.canonical_payload == "create long-form cinematic YouTube videos"
        assert session.scalar(select(func.count(MemoryTombstoneV2.id))) == 0
        assert session.scalar(select(func.count(MemoryOperationV2.id))) == 1
        assert session.scalar(select(func.count(MemoryOutboxV2.id))) == 1
