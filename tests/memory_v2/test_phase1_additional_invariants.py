from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.db.memory_v2_migrations import (
    MEMORY_V2_CURRENT_REVISION,
    MEMORY_V2_LEDGER_TABLE,
    MemoryV2MigrationError,
    memory_v2_migration_state,
    upgrade_memory_v2,
)
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryRelationV2,
)
from app.repositories.memory_v2 import MemoryV2ProhibitedContentError, MemoryV2Repository
from app.services.memory_v2.contracts import (
    CandidateIntent,
    CandidateLifecycleState,
    Sensitivity,
)
from app.services.memory_v2.diagnostics import (
    canonical_data_checksum,
    inspect_memory_v2_invariants,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from app.services.memory_v2.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION
from tests.memory_v2.factories import (
    DATABASE_IDENTITY,
    OWNER_A,
    OWNER_B,
    operation,
    record,
    uuid_string,
)


def _candidate(number: int = 500) -> MemoryCandidateV2:
    return MemoryCandidateV2(
        id=uuid_string(number),
        owner_id=OWNER_A,
        subject_key="user",
        memory_type=MemoryType.KNOWLEDGE.value,
        domain_key="software_development",
        slot_key=f"knowledge:software_development:item:{uuid_string(number + 1)}",
        cardinality=Cardinality.ADDITIVE.value,
        sensitivity=Sensitivity.NORMAL.value,
        canonical_payload="Use deterministic tests",
        display_text="Use deterministic tests",
        intent=CandidateIntent.ASSERT.value,
        target_hints_json={},
        trusted_target_ids=[],
        predecessor_evidence_json={},
        source_spans_json=[],
        grounding_evidence_json={},
        confidence=1,
        importance=7,
        explicit_user_request=False,
        extractor_name="test",
        extractor_version="1",
        state=CandidateLifecycleState.PROPOSED.value,
        revision=1,
        contract_version=CONTRACT_VERSION,
        policy_version=POLICY_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        value_schema_version=1,
        candidate_schema_version=1,
    )


def test_duplicate_relation_identity_is_rejected(memory_v2_session) -> None:
    memory_v2_session.add(operation(number=100))
    memory_v2_session.add_all(
        [
            record(number=200),
            record(number=201, status="archived"),
        ]
    )
    memory_v2_session.flush()
    memory_v2_session.add_all(
        [
            MemoryRelationV2(
                id=uuid_string(400),
                owner_id=OWNER_A,
                from_memory_id=uuid_string(200),
                relation_type="supersedes",
                to_memory_id=uuid_string(201),
                operation_id=uuid_string(100),
                schema_version=1,
            ),
            MemoryRelationV2(
                id=uuid_string(401),
                owner_id=OWNER_A,
                from_memory_id=uuid_string(200),
                relation_type="supersedes",
                to_memory_id=uuid_string(201),
                operation_id=uuid_string(100),
                schema_version=1,
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        memory_v2_session.flush()


@pytest.mark.parametrize(
    "values",
    [
        {"event_kind": "canonical_upsert", "memory_id": None},
        {"event_kind": "usage", "memory_id": uuid_string(200), "canonical_revision": 0},
        {"event_kind": "reconciliation_request", "attempts": -1},
        {"event_kind": "reconciliation_request", "schema_version": 0},
    ],
)
def test_outbox_required_record_revision_and_bounds_are_enforced(memory_v2_session, values) -> None:
    memory_v2_session.add(operation(number=100))
    memory_v2_session.add(record(number=200))
    memory_v2_session.flush()
    payload = {
        "id": uuid_string(600),
        "owner_id": OWNER_A,
        "event_kind": "reconciliation_request",
        "memory_id": None,
        "canonical_revision": None,
        "event_payload_json": {},
        "state": "pending",
        "attempts": 0,
        "event_idempotency_key": "event-600",
        "schema_version": 1,
    }
    payload.update(values)
    memory_v2_session.add(MemoryOutboxV2(**payload))
    with pytest.raises(IntegrityError):
        memory_v2_session.flush()


def test_repository_rollback_remains_caller_controlled(memory_v2_session) -> None:
    repository = MemoryV2Repository(
        memory_v2_session,
        owner_id=OWNER_A,
        database_identity=DATABASE_IDENTITY,
    )
    repository.add_operation(operation(number=100))
    assert memory_v2_session.scalar(select(func.count(MemoryOperationV2.id))) == 1

    memory_v2_session.rollback()

    assert memory_v2_session.scalar(select(func.count(MemoryOperationV2.id))) == 0


def test_repository_allows_structural_uuids_but_scans_semantic_payloads(
    memory_v2_session,
) -> None:
    repository = MemoryV2Repository(
        memory_v2_session,
        owner_id=OWNER_A,
        database_identity=DATABASE_IDENTITY,
    )
    structural = operation(number=100)
    structural.normalized_command_json = {
        "owner_id": OWNER_A,
        "target_memory_id": uuid_string(200),
    }
    repository.add_operation(structural)

    semantic = operation(number=101)
    semantic.normalized_command_json = {"instruction": "password is [redacted]"}
    with pytest.raises(
        MemoryV2ProhibitedContentError,
        match="^prohibited_content_not_persisted$",
    ):
        repository.add_operation(semantic)


def test_migration_state_records_order_and_detects_missing_managed_schema(
    memory_v2_engine,
) -> None:
    state = memory_v2_migration_state(memory_v2_engine)
    assert state.current_revision == MEMORY_V2_CURRENT_REVISION
    assert state.applied_revisions == (MEMORY_V2_CURRENT_REVISION,)
    with memory_v2_engine.connect() as connection:
        ledger = connection.execute(
            text(f"SELECT revision, revision_checksum, applied_at FROM {MEMORY_V2_LEDGER_TABLE}")
        ).one()
    assert ledger.revision == MEMORY_V2_CURRENT_REVISION
    assert len(ledger.revision_checksum) == 64
    assert ledger.applied_at is not None

    with memory_v2_engine.begin() as connection:
        connection.execute(text("DROP TABLE memory_candidates_v2"))
    with pytest.raises(MemoryV2MigrationError, match="memory_v2_schema_missing_tables"):
        upgrade_memory_v2(
            memory_v2_engine,
            owner_id=OWNER_A,
            database_identity=DATABASE_IDENTITY,
        )


def test_diagnostics_report_all_security_gaps_and_never_repair_them(
    memory_v2_engine,
) -> None:
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=memory_v2_engine, autoflush=False, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                operation(owner_id=OWNER_A, number=100),
                operation(owner_id=OWNER_B, number=101),
            ]
        )
        session.flush()
        session.add_all(
            [
                record(owner_id=OWNER_A, number=200),
                record(owner_id=OWNER_A, number=201, status="archived"),
                record(
                    owner_id=OWNER_B,
                    number=202,
                    operation_id=uuid_string(101),
                    slot_key="goal:learning:current_primary_goal",
                ),
                _candidate(),
            ]
        )
        session.flush()
        session.add_all(
            [
                MemoryOutboxV2(
                    id=uuid_string(600),
                    owner_id=OWNER_A,
                    event_kind="canonical_upsert",
                    memory_id=uuid_string(200),
                    canonical_revision=1,
                    content_hash="content-hash",
                    event_payload_json={},
                    state="failed",
                    attempts=1,
                    last_error="deterministic test failure",
                    event_idempotency_key="failed-600",
                    schema_version=1,
                ),
                MemoryRelationV2(
                    id=uuid_string(400),
                    owner_id=OWNER_A,
                    from_memory_id=uuid_string(200),
                    relation_type="supersedes",
                    to_memory_id=uuid_string(201),
                    operation_id=uuid_string(100),
                    schema_version=1,
                ),
            ]
        )

    database_path = memory_v2_engine.url.database
    memory_v2_engine.dispose()
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE memory_candidates_v2 SET display_text = NULL WHERE id = ?",
            (uuid_string(500),),
        )
        connection.execute(
            "UPDATE memory_relations_v2 SET to_memory_id = from_memory_id WHERE id = ?",
            (uuid_string(400),),
        )
        connection.execute(
            "INSERT INTO memory_relations_v2 "
            "(id, owner_id, from_memory_id, relation_type, to_memory_id, operation_id, "
            "schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid_string(401),
                OWNER_A,
                uuid_string(200),
                "refines",
                uuid_string(999),
                uuid_string(100),
                1,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute(
            "INSERT INTO memory_relations_v2 "
            "(id, owner_id, from_memory_id, relation_type, to_memory_id, operation_id, "
            "schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid_string(402),
                OWNER_A,
                uuid_string(200),
                "refines",
                uuid_string(202),
                uuid_string(100),
                1,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    before = canonical_data_checksum(memory_v2_engine, owner_id=OWNER_A)
    first = inspect_memory_v2_invariants(memory_v2_engine, owner_id=OWNER_A)
    second = inspect_memory_v2_invariants(memory_v2_engine, owner_id=OWNER_A)
    after = canonical_data_checksum(memory_v2_engine, owner_id=OWNER_A)

    assert first.failed_outbox == 1
    assert first.pending_outbox == 0
    assert not first.healthy
    assert {violation.code for violation in first.violations} >= {
        "invalid_candidate_payload_shape",
        "orphan_cross_owner_or_self_relation",
    }
    assert first == second
    assert before == after
