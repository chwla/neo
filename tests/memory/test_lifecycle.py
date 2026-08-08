from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryOutbox,
    MemoryRecord,
    MemorySource,
    MemoryTombstone,
)
from app.services.memory.contracts import (
    ArchiveMemoryCommand,
    CandidateIntent,
    CreateMemoryCommand,
    ErasePermanentlyMemoryCommand,
    ForgetMemoryCommand,
    MemoryOutcome,
    MemoryRejectionCode,
    RestoreMemoryCommand,
    RestoreMode,
    TargetRevision,
)
from tests.memory.helpers import OWNER_A, actor, candidate, source


def _create(service, value: str, *, key: str, explicit: bool = False):
    return service.execute(
        CreateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key=key,
            actor=actor(),
            source=source(),
            candidate=candidate(value, explicit=explicit),
        )
    )


def test_archive_and_restore_into_empty_slot(mutation_service, phase2_engine) -> None:
    created = _create(mutation_service, "create tutorial videos", key="archive-create")
    memory_id = created.active_memory_ids[0]
    archived = mutation_service.execute(
        ArchiveMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="archive-command",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
        )
    )
    restored = mutation_service.execute(
        RestoreMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="restore-command",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=2),
        )
    )

    assert archived.outcome is MemoryOutcome.ARCHIVED
    assert restored.outcome is MemoryOutcome.RESTORED
    assert restored.active_memory_ids == (memory_id,)
    with Session(phase2_engine) as session:
        record = session.get(MemoryRecord, str(memory_id))
        assert record is not None and record.status == "active" and record.revision == 3
        event_kinds = list(session.scalars(select(MemoryOutbox.event_kind)))
        assert event_kinds.count("canonical_remove") == 1
        assert event_kinds.count("canonical_upsert") == 2


def test_unsafe_restore_fails_and_restore_as_replacement_creates_new_version(
    mutation_service,
    phase2_engine,
) -> None:
    historical = _create(
        mutation_service,
        "create tutorial videos",
        key="unsafe-historical",
    )
    historical_id = historical.active_memory_ids[0]
    mutation_service.execute(
        ArchiveMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="unsafe-archive",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=historical_id, expected_revision=1),
        )
    )
    current = _create(
        mutation_service,
        "create short cinematic videos",
        key="unsafe-current",
    )
    unsafe = mutation_service.execute(
        RestoreMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="unsafe-restore",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=historical_id, expected_revision=2),
        )
    )
    intentional = mutation_service.execute(
        RestoreMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="restore-as-replacement",
            actor=actor(),
            source=source(include_correction_evidence=True),
            target=TargetRevision(memory_id=historical_id, expected_revision=2),
            mode=RestoreMode.AS_REPLACEMENT,
            replacement_candidate=candidate(
                "create tutorial videos",
                intent=CandidateIntent.REPLACE,
                targets=(current.active_memory_ids[0],),
            ),
        )
    )

    assert unsafe.outcome is MemoryOutcome.REJECTED
    assert unsafe.rejection_code is MemoryRejectionCode.INVALID_RESTORE
    assert intentional.outcome is MemoryOutcome.RESTORED
    assert intentional.active_memory_ids[0] not in {historical_id, current.active_memory_ids[0]}
    with Session(phase2_engine) as session:
        old = session.get(MemoryRecord, str(historical_id))
        previous_current = session.get(MemoryRecord, str(current.active_memory_ids[0]))
        assert old is not None and old.status == "archived"
        assert previous_current is not None and previous_current.status == "superseded"


def test_forget_blocks_automatic_resurrection_and_allows_explicit_reconfirmation(
    mutation_service,
    phase2_engine,
) -> None:
    value = "create tutorial videos"
    created = _create(mutation_service, value, key="forget-create")
    memory_id = created.active_memory_ids[0]
    forgotten = mutation_service.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="forget-command",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
        )
    )
    blocked = _create(mutation_service, value, key="automatic-resurrection")
    explicit = _create(
        mutation_service,
        value,
        key="explicit-reconfirmation",
        explicit=True,
    )

    assert forgotten.outcome is MemoryOutcome.FORGOTTEN
    assert blocked.outcome is MemoryOutcome.REJECTED
    assert blocked.rejection_code is MemoryRejectionCode.RESURRECTION_BLOCKED
    assert explicit.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        old = session.get(MemoryRecord, str(memory_id))
        assert old is not None and old.status == "forgotten"
        assert old.canonical_payload is None and old.display_text is None
        assert old.encrypted_canonical_payload is not None
        tombstone = session.scalar(select(MemoryTombstone))
        assert tombstone is not None
        assert tombstone.explicitly_reconfirmed
        assert value not in tombstone.fingerprint_digest
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "active")
            )
            == 1
        )
        assert "tombstone_expiry" in list(session.scalars(select(MemoryOutbox.event_kind)))


def test_expired_tombstone_does_not_block_recreation(mutation_service, phase2_engine) -> None:
    value = "create tutorial videos"
    created = _create(mutation_service, value, key="expiry-create")
    mutation_service.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="expiry-forget",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=created.active_memory_ids[0], expected_revision=1),
        )
    )
    with phase2_engine.begin() as connection:
        connection.execute(
            update(MemoryTombstone).values(
                created_at=datetime.now(UTC) - timedelta(days=31),
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
    recreated = _create(mutation_service, value, key="after-expiry")
    assert recreated.outcome is MemoryOutcome.CREATED


def test_permanent_erasure_removes_value_provenance_tombstone_and_blocking_fingerprint(
    mutation_service,
    phase2_engine,
) -> None:
    value = "create tutorial videos"
    created = _create(mutation_service, value, key="erase-create")
    memory_id = created.active_memory_ids[0]
    mutation_service.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="erase-forget",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
        )
    )
    erased = mutation_service.execute(
        ErasePermanentlyMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="erase-permanently",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=2),
        )
    )
    recreated = _create(mutation_service, value, key="recreate-after-erase")

    assert erased.outcome is MemoryOutcome.ERASED_PERMANENTLY
    assert recreated.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        assert session.get(MemoryRecord, str(memory_id)) is None
        assert session.scalar(select(func.count(MemoryTombstone.id))) == 0
        assert (
            session.scalar(
                select(func.count(MemorySource.id)).where(MemorySource.memory_id == str(memory_id))
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count(MemoryCandidate.id)).where(
                    MemoryCandidate.applied_operation_id == str(created.operation_id)
                )
            )
            == 0
        )
        creating_operation = session.get(MemoryOperation, str(created.operation_id))
        assert creating_operation is not None
        assert creating_operation.normalized_command_json == {
            "operation": "create",
            "redacted_by_permanent_erasure": True,
        }
        assert creating_operation.request_hash == f"erased:{creating_operation.id}"
        reconciliation = session.scalar(
            select(MemoryOutbox).where(MemoryOutbox.event_kind == "reconciliation_request")
        )
        assert reconciliation is not None
        assert reconciliation.memory_id is None


def test_permanent_erasure_removes_reconfirmation_candidates(
    mutation_service,
    phase2_engine,
) -> None:
    value = "create tutorial videos"
    created = _create(mutation_service, value, key="erase-reconfirm-create")
    reconfirmed = _create(mutation_service, value, key="erase-reconfirm-duplicate")
    memory_id = created.active_memory_ids[0]
    assert reconfirmed.outcome is MemoryOutcome.RECONFIRMED
    mutation_service.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="erase-reconfirm-forget",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=2),
        )
    )
    erased = mutation_service.execute(
        ErasePermanentlyMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="erase-reconfirm-permanent",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=3),
        )
    )
    assert erased.outcome is MemoryOutcome.ERASED_PERMANENTLY
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryCandidate.id))) == 0
