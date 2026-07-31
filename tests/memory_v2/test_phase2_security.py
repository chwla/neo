from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.memory_v2_migrations import upgrade_memory_v2
from app.db.session import build_engine
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemorySourceV2,
)
from app.repositories.memory_v2 import MemoryV2ProhibitedContentError, MemoryV2Repository
from app.services.memory_v2.contracts import (
    CandidateIntent,
    CreateMemoryCommand,
    EvidenceRole,
    EvidenceSpan,
    MemoryErrorCode,
    MemoryOutcome,
    MemoryRejectionCode,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    Sensitivity,
    TargetRevision,
)
from app.services.memory_v2.mutations import (
    MemoryMutationError,
    MemoryMutationService,
    RetryPolicy,
)
from app.services.memory_v2.policy import classify_sensitivity
from app.services.memory_v2.tombstones import TombstoneSnapshot, resurrection_blocked
from tests.memory_v2.helpers import (
    OWNER_A,
    OWNER_B,
    actor,
    candidate,
    source,
)


def _service(engine, owner_id: str, database_identity: str, crypto):
    return MemoryMutationService(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
        retry_policy=RetryPolicy(attempts=3, base_delay_seconds=0),
    )


def test_sensitive_record_candidate_command_and_evidence_are_opaque(
    mutation_service,
    phase2_engine,
) -> None:
    private_value = "private health preference"
    private_source = source().model_copy(
        update={"evidence": (EvidenceSpan(role=EvidenceRole.ASSERTION, text=private_value),)}
    )
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="sensitive-create",
        actor=actor(),
        source=private_source,
        candidate=candidate(
            private_value,
            display="private health preference",
            sensitivity=Sensitivity.SENSITIVE,
            explicit=True,
        ),
    )
    result = mutation_service.execute(command)
    assert result.outcome is MemoryOutcome.CREATED

    with Session(phase2_engine) as session:
        record = session.get(MemoryRecordV2, str(result.active_memory_ids[0]))
        operation = session.get(MemoryOperationV2, str(result.operation_id))
        stored_candidate = session.get(MemoryCandidateV2, str(result.candidate_id))
        stored_source = session.scalar(
            select(MemorySourceV2).where(MemorySourceV2.operation_id == str(result.operation_id))
        )
        assert record is not None and operation is not None and stored_candidate is not None
        assert stored_source is not None
        assert record.canonical_payload is None and record.display_text is None
        assert record.encrypted_canonical_payload is not None
        assert private_value.encode() not in record.encrypted_canonical_payload
        assert record.canonical_fingerprint.startswith("keyed:test-fingerprint-v1:")
        assert stored_candidate.canonical_payload is None
        assert stored_candidate.encrypted_canonical_payload is not None
        assert operation.normalized_command_json is None
        assert operation.encrypted_command_payload is not None
        assert private_value.encode() not in operation.encrypted_command_payload
        assert stored_source.redacted_excerpt is None
        assert stored_source.encrypted_excerpt is not None
        assert private_value.encode() not in stored_source.encrypted_excerpt
        aad = record.encryption_aad.decode()
        assert OWNER_A in aad
        assert record.id in aad
        assert record.memory_type in aad
        assert record.domain_key in aad
        assert record.slot_key in aad
        outbox_payloads = session.scalars(select(MemoryOutboxV2.event_payload_json)).all()
        assert private_value not in json_text(outbox_payloads)


def test_prohibited_material_is_rejected_before_candidate_or_record_persistence(
    mutation_service,
    phase2_engine,
) -> None:
    proposal = candidate("ordinary placeholder").model_copy(
        update={
            "canonical_value": "password is [redacted]",
            "display_text": "password is [redacted]",
            "sensitivity": Sensitivity.PROHIBITED,
        }
    )
    normal_command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="prohibited-rejection",
        actor=actor(),
        source=source(),
        candidate=candidate("ordinary placeholder"),
    )
    command = normal_command.model_copy(update={"candidate": proposal})
    result = mutation_service.execute(command)

    assert result.outcome is MemoryOutcome.REJECTED
    assert result.rejection_code is MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT
    assert result.message == "prohibited_content_not_persisted"
    assert "password" not in repr(result).casefold()
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 0
        assert session.scalar(select(func.count(MemoryCandidateV2.id))) == 0
        operation = session.scalar(select(MemoryOperationV2))
        assert operation is not None
        assert operation.normalized_command_json == {
            "contract_version": command.contract_version,
            "idempotency_key": command.idempotency_key,
            "operation": "create",
            "owner_id": OWNER_A,
            "policy_version": command.policy_version,
            "redacted": True,
            "taxonomy_version": command.taxonomy_version,
        }
        assert "password" not in json_text(operation.normalized_command_json).casefold()


def json_text(value) -> str:
    import json

    return json.dumps(value, sort_keys=True)


def test_invalid_command_shape_uses_fixed_non_echoing_error(
    mutation_service,
    phase2_engine,
) -> None:
    secret = "private-input-that-must-not-echo"
    with pytest.raises(MemoryMutationError) as raised:
        mutation_service.execute(
            {
                "operation": "create",
                "owner_id": OWNER_A,
                "idempotency_key": "invalid-shape",
                "candidate": {"display_text": secret},
            }
        )
    assert str(raised.value) == "invalid_command_shape"
    assert secret not in repr(raised.value)
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryOperationV2.id))) == 0


def test_repository_content_guard_ignores_structural_uuids_but_not_payloads() -> None:
    structural = {
        "expires_at": "2026-08-30T14:04:30.117562+00:00",
        "tombstone_id": "d1751398-0639-5749-9952-3c26ad9777f9",
    }
    assert classify_sensitivity(json_text(structural)) is Sensitivity.PROHIBITED
    MemoryV2Repository._reject_prohibited_material(structural)
    with pytest.raises(MemoryV2ProhibitedContentError):
        MemoryV2Repository._reject_prohibited_material({"canonical_value": "4111 1111 1111 1111"})


def test_tombstone_comparison_is_owner_bound(test_crypto) -> None:
    fingerprint = "sha256:opaque-test-fingerprint"
    digest = test_crypto.create(
        f"neo.memory.tombstone.v1\0{fingerprint}".encode(),
        owner_id=OWNER_A,
    )
    now = datetime.now(UTC)
    item = TombstoneSnapshot(
        id="00000000-0000-4000-8000-000000000700",
        owner_id=OWNER_A,
        fingerprint_digest=digest.digest,
        fingerprint_key_version=digest.key_version,
        memory_type="goal",
        domain_key="video_creation",
        slot_key="goal:video_creation:current_primary_goal",
        created_at=now,
        expires_at=now + timedelta(days=30),
        explicitly_reconfirmed=False,
    )
    assert (
        resurrection_blocked(
            (item,),
            fingerprint,
            owner_id=OWNER_A,
            now=now,
            explicit_reconfirmation=False,
            provider=test_crypto,
        )
        is item
    )
    assert (
        resurrection_blocked(
            (item,),
            fingerprint,
            owner_id=OWNER_B,
            now=now,
            explicit_reconfirmation=False,
            provider=test_crypto,
        )
        is None
    )


def test_cross_owner_target_fails_as_not_found(
    mutation_service,
    tmp_path,
    test_crypto,
) -> None:
    other_identity = "phase2-other-profile"
    other_engine = build_engine(f"sqlite:///{tmp_path / 'other-owner.db'}")
    upgrade_memory_v2(other_engine, owner_id=OWNER_B, database_identity=other_identity)
    try:
        other = _service(other_engine, OWNER_B, other_identity, test_crypto)
        other_created = other.execute(
            CreateMemoryCommand(
                owner_id=OWNER_B,
                idempotency_key="other-owner-create",
                actor=actor(),
                source=source(),
                candidate=candidate("create tutorial videos"),
            )
        )
        cross_owner_id = other_created.active_memory_ids[0]
        result = mutation_service.execute(
            ReplaceMemoryCommand(
                owner_id=OWNER_A,
                idempotency_key="cross-owner-target",
                actor=actor(),
                source=source(),
                candidate=candidate(
                    "create short Instagram reels clearly",
                    intent=CandidateIntent.REPLACE,
                ),
                authority=ReplacementAuthority.EXPLICIT_CORRECTION,
                targets=(TargetRevision(memory_id=cross_owner_id, expected_revision=1),),
            )
        )
        assert result.outcome is MemoryOutcome.FAILED
        assert result.error_code is MemoryErrorCode.NOT_FOUND
        assert str(cross_owner_id) not in (result.message or "")
    finally:
        other_engine.dispose()
