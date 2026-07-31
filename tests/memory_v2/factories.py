from __future__ import annotations

from datetime import UTC, datetime

from app.models.memory_v2 import MemoryOperationV2, MemoryRecordV2
from app.services.memory_v2.contracts import (
    ActorKind,
    MemoryLifecycleState,
    MemoryOperationKind,
    Sensitivity,
    SourceKind,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from app.services.memory_v2.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION

OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"
DATABASE_IDENTITY = "test-profile:one"


def uuid_string(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def operation(
    *,
    owner_id: str = OWNER_A,
    number: int = 100,
    idempotency_key: str | None = None,
    operation_kind: str = MemoryOperationKind.CREATE.value,
) -> MemoryOperationV2:
    return MemoryOperationV2(
        id=uuid_string(number),
        owner_id=owner_id,
        idempotency_key=idempotency_key or f"operation-{number}",
        operation_kind=operation_kind,
        actor_kind=ActorKind.USER.value,
        actor_id="test-user",
        source_kind=SourceKind.DIRECT_COMMAND.value,
        sensitivity=Sensitivity.NORMAL.value,
        normalized_command_json={"operation": operation_kind},
        encrypted_command_payload=None,
        encryption_algorithm=None,
        encryption_key_version=None,
        encryption_nonce=None,
        encryption_aad=None,
        request_hash=f"request-hash-{number}",
        status="started",
        outcome=None,
        rejection_code=None,
        error_code=None,
        result_record_ids=[],
        contract_version=CONTRACT_VERSION,
        policy_version=POLICY_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        schema_version=1,
    )


def record(
    *,
    owner_id: str = OWNER_A,
    number: int = 200,
    operation_id: str | None = None,
    slot_key: str = "goal:video_creation:primary_output",
    fingerprint: str | None = None,
    cardinality: str = Cardinality.EXCLUSIVE.value,
    status: str = MemoryLifecycleState.ACTIVE.value,
    sensitivity: str = Sensitivity.NORMAL.value,
    canonical_payload: object | None = "create short Instagram reels clearly",
    display_text: str | None = "create short Instagram reels clearly",
) -> MemoryRecordV2:
    now = datetime.now(UTC)
    sensitive = sensitivity == Sensitivity.SENSITIVE.value
    return MemoryRecordV2(
        id=uuid_string(number),
        owner_id=owner_id,
        subject_key="user",
        memory_type=MemoryType.GOAL.value,
        domain_key="video_creation",
        slot_key=slot_key,
        cardinality=cardinality,
        sensitivity=sensitivity,
        canonical_payload=None if sensitive else canonical_payload,
        display_text=None if sensitive else display_text,
        encrypted_canonical_payload=b"opaque-canonical" if sensitive else None,
        encrypted_display_payload=b"opaque-display" if sensitive else None,
        encryption_algorithm="opaque-aead-v1" if sensitive else None,
        encryption_key_version="key-v1" if sensitive else None,
        canonical_nonce=b"canonical-nonce" if sensitive else None,
        display_nonce=b"display-nonce" if sensitive else None,
        encryption_aad=b"authenticated-metadata" if sensitive else None,
        canonical_fingerprint=fingerprint or f"fingerprint-{number}",
        confidence=0.95,
        importance=7,
        status=status,
        last_confirmed_at=now,
        usage_count=0,
        pinned=False,
        created_by_operation_id=operation_id or uuid_string(100),
        revision=1,
        metadata_json={},
        contract_version=CONTRACT_VERSION,
        taxonomy_version=TAXONOMY_VERSION,
        policy_version=POLICY_VERSION,
        value_schema_version=1,
        record_schema_version=1,
    )
