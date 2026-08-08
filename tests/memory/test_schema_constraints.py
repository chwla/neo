from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.memory import (
    MemoryCandidate,
    MemoryOutbox,
    MemoryRelation,
    MemorySource,
    MemoryTombstone,
)
from app.services.memory.contracts import (
    CandidateIntent,
    CandidateLifecycleState,
    MemoryOperationKind,
    Sensitivity,
    SourceKind,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from app.services.memory.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION
from tests.memory.factories import OWNER_A, OWNER_B, operation, record, uuid_string


def _factory(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def test_two_active_exclusive_records_same_owner_slot_are_rejected(memory_engine) -> None:
    factory = _factory(memory_engine)
    first = factory()
    try:
        first.add_all([operation(number=100), record(number=200)])
        first.commit()
    finally:
        first.close()

    second = factory()
    try:
        second.add(operation(number=101))
        second.add(record(number=201, operation_id=uuid_string(101)))
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
    finally:
        second.close()


def test_same_exclusive_slot_is_allowed_for_different_owners(memory_session) -> None:
    memory_session.add_all(
        [
            operation(owner_id=OWNER_A, number=100),
            operation(owner_id=OWNER_B, number=101),
            record(owner_id=OWNER_A, number=200, fingerprint="owner-a-fingerprint"),
            record(
                owner_id=OWNER_B,
                number=201,
                operation_id=uuid_string(101),
                fingerprint="owner-b-fingerprint",
            ),
        ]
    )
    memory_session.flush()


def test_independent_additive_goals_are_allowed(memory_session) -> None:
    memory_session.add(operation(number=100))
    memory_session.add_all(
        [
            record(
                number=200,
                slot_key="goal:learning:independent:00000000-0000-4000-8000-000000000501",
                cardinality=Cardinality.ADDITIVE.value,
            ),
            record(
                number=201,
                slot_key="goal:learning:independent:00000000-0000-4000-8000-000000000502",
                cardinality=Cardinality.ADDITIVE.value,
            ),
        ]
    )
    memory_session.flush()


def test_duplicate_active_fingerprint_same_owner_is_rejected(memory_session) -> None:
    memory_session.add(operation(number=100))
    memory_session.add_all(
        [
            record(number=200, fingerprint="same-fingerprint"),
            record(
                number=201,
                slot_key="goal:video_creation:current_primary_goal",
                fingerprint="same-fingerprint",
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_same_active_fingerprint_is_allowed_for_different_owners(memory_session) -> None:
    memory_session.add_all(
        [
            operation(owner_id=OWNER_A, number=100),
            operation(owner_id=OWNER_B, number=101),
            record(owner_id=OWNER_A, number=200, fingerprint="same-fingerprint"),
            record(
                owner_id=OWNER_B,
                number=201,
                operation_id=uuid_string(101),
                fingerprint="same-fingerprint",
            ),
        ]
    )
    memory_session.flush()


def test_cross_owner_source_insert_is_rejected(memory_session) -> None:
    now = datetime.now(UTC)
    memory_session.add_all(
        [
            operation(owner_id=OWNER_A, number=100),
            operation(owner_id=OWNER_B, number=101),
            record(
                owner_id=OWNER_B,
                number=201,
                operation_id=uuid_string(101),
            ),
        ]
    )
    memory_session.flush()
    memory_session.add(
        MemorySource(
            id=uuid_string(300),
            owner_id=OWNER_A,
            memory_id=uuid_string(201),
            source_kind=SourceKind.CHAT_MESSAGE.value,
            source_content_hash="source-hash",
            observed_at=now,
            assertion_role="supports",
            is_active=True,
            operation_id=uuid_string(100),
            schema_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_cross_owner_relation_and_self_relation_are_rejected(memory_engine) -> None:
    factory = _factory(memory_engine)
    session = factory()
    try:
        session.add_all(
            [
                operation(owner_id=OWNER_A, number=100),
                operation(owner_id=OWNER_B, number=101),
                record(owner_id=OWNER_A, number=200),
                record(
                    owner_id=OWNER_B,
                    number=201,
                    operation_id=uuid_string(101),
                ),
            ]
        )
        session.commit()
        session.add(
            MemoryRelation(
                id=uuid_string(400),
                owner_id=OWNER_A,
                from_memory_id=uuid_string(200),
                relation_type="supersedes",
                to_memory_id=uuid_string(201),
                operation_id=uuid_string(100),
                schema_version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

        session.add(
            MemoryRelation(
                id=uuid_string(401),
                owner_id=OWNER_A,
                from_memory_id=uuid_string(200),
                relation_type="supersedes",
                to_memory_id=uuid_string(200),
                operation_id=uuid_string(100),
                schema_version=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
    finally:
        session.rollback()
        session.close()


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: setattr(item, "encrypted_canonical_payload", b"also-encrypted"),
        lambda item: setattr(item, "display_text", ""),
        lambda item: setattr(item, "sensitivity", Sensitivity.PROHIBITED.value),
    ],
)
def test_invalid_normal_sensitive_or_prohibited_record_is_rejected(memory_session, mutator) -> None:
    memory_session.add(operation(number=100))
    item = record(number=200)
    mutator(item)
    memory_session.add(item)
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_opaque_sensitive_payload_shape_is_accepted(memory_session) -> None:
    memory_session.add(operation(number=100))
    item = record(number=200, sensitivity=Sensitivity.SENSITIVE.value)
    memory_session.add(item)
    memory_session.flush()
    assert item.canonical_payload is None
    assert item.display_text is None
    assert item.encrypted_canonical_payload == b"opaque-canonical"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "unknown"),
        ("cardinality", "sometimes"),
        ("revision", 0),
        ("record_schema_version", 0),
        ("value_schema_version", 0),
        ("confidence", 1.1),
        ("importance", 0),
        ("usage_count", -1),
    ],
)
def test_invalid_record_states_ranges_and_versions_are_rejected(
    memory_session, field: str, value
) -> None:
    memory_session.add(operation(number=100))
    item = record(number=200)
    setattr(item, field, value)
    memory_session.add(item)
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_prohibited_candidate_cannot_be_persisted(memory_session) -> None:
    memory_session.add(
        MemoryCandidate(
            id=uuid_string(500),
            owner_id=OWNER_A,
            subject_key="user",
            memory_type=MemoryType.KNOWLEDGE.value,
            domain_key="software_development",
            slot_key=f"knowledge:software_development:item:{uuid_string(501)}",
            cardinality=Cardinality.ADDITIVE.value,
            sensitivity=Sensitivity.PROHIBITED.value,
            canonical_payload="redacted-test-marker",
            display_text="redacted-test-marker",
            intent=CandidateIntent.ASSERT.value,
            target_hints_json={},
            trusted_target_ids=[],
            predecessor_evidence_json={},
            source_spans_json=[],
            grounding_evidence_json={},
            confidence=1,
            importance=10,
            explicit_user_request=True,
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
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_operation_idempotency_is_unique_per_owner(memory_session) -> None:
    memory_session.add_all(
        [
            operation(number=100, idempotency_key="same-key"),
            operation(number=101, idempotency_key="same-key"),
        ]
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_invalid_outbox_state_is_rejected(memory_session) -> None:
    memory_session.add(
        MemoryOutbox(
            id=uuid_string(600),
            owner_id=OWNER_A,
            event_kind="usage",
            state="invented",
            attempts=0,
            event_idempotency_key="usage-event-1",
            schema_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()


def test_tombstone_has_no_plaintext_columns_and_expiry_must_follow_creation(
    memory_engine, memory_session
) -> None:
    columns = {column["name"] for column in inspect(memory_engine).get_columns("memory_tombstones")}
    assert not ({"canonical_payload", "display_text", "memory_text", "canonical_value"} & columns)

    now = datetime.now(UTC)
    memory_session.add(operation(number=100, operation_kind=MemoryOperationKind.FORGET.value))
    memory_session.add(
        MemoryTombstone(
            id=uuid_string(700),
            owner_id=OWNER_A,
            fingerprint_digest="opaque-hmac-digest",
            fingerprint_key_version="hmac-key-v1",
            memory_type=MemoryType.GOAL.value,
            domain_key="video_creation",
            slot_key="goal:video_creation:primary_output",
            originating_operation_id=uuid_string(100),
            created_at=now,
            expires_at=now - timedelta(seconds=1),
            explicitly_reconfirmed=False,
            schema_version=1,
        )
    )
    with pytest.raises(IntegrityError):
        memory_session.flush()
