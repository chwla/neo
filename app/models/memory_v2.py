"""Phase 1 ORM schema for Neo personal memory v2.

These models use a dedicated metadata object so legacy ``Base.metadata.create_all``
cannot create v2 tables. Production upgrades must use the explicit v2 migration ledger.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.services.memory_v2.contracts import (
    ActorKind,
    CandidateIntent,
    CandidateLifecycleState,
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    Sensitivity,
    SourceKind,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryType

UUID_LENGTH = 36
FINGERPRINT_LENGTH = 128
RECORD_SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 1
SOURCE_SCHEMA_VERSION = 1
RELATION_SCHEMA_VERSION = 1
OPERATION_SCHEMA_VERSION = 1
OUTBOX_SCHEMA_VERSION = 1
TOMBSTONE_SCHEMA_VERSION = 1
MIGRATION_SCHEMA_VERSION = 1

RELATION_TYPES = ("supersedes", "refines", "merged_from", "duplicate_of")
OPERATION_STATUSES = ("started", "committed", "rejected", "failed")
OUTBOX_EVENT_KINDS = (
    "canonical_upsert",
    "canonical_remove",
    "usage",
    "tombstone_expiry",
    "reconciliation_request",
)
OUTBOX_STATES = ("pending", "processing", "done", "failed")
SOURCE_ASSERTION_ROLES = ("supports", "retracts_predecessor", "restores", "edits_source")
MIGRATION_RUN_PHASES = ("preflight", "schema", "normalize", "apply", "validate", "complete")
MIGRATION_RUN_STATUSES = ("pending", "running", "completed", "failed", "rolled_back")
LEGACY_MIGRATION_OUTCOMES = (
    "pending",
    "migrated_active",
    "migrated_history",
    "merged_duplicate",
    "quarantined",
    "excluded",
    "failed",
)


class MemoryV2Base(DeclarativeBase):
    """Dedicated declarative base used only by explicit v2 migrations."""


def _quoted(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _enum_check(column: str, values: tuple[str, ...] | list[str], name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN ({_quoted(values)})", name=name)


def _nullable_enum_check(
    column: str,
    values: tuple[str, ...] | list[str],
    name: str,
) -> CheckConstraint:
    return CheckConstraint(
        f"{column} IS NULL OR {column} IN ({_quoted(values)})",
        name=name,
    )


def _uuid_check(column: str, name: str) -> CheckConstraint:
    compact = f"replace({column}, '-', '')"
    return CheckConstraint(
        f"length({column}) = 36 AND {column} = lower({column}) "
        f"AND substr({column}, 9, 1) = '-' AND substr({column}, 14, 1) = '-' "
        f"AND substr({column}, 19, 1) = '-' AND substr({column}, 24, 1) = '-' "
        f"AND length({compact}) = 32 AND {compact} NOT GLOB '*[^0-9a-f]*'",
        name=name,
    )


def _payload_shape_check(prefix: str, name: str) -> CheckConstraint:
    canonical = f"{prefix}canonical_payload"
    display = f"{prefix}display_text"
    encrypted_canonical = f"{prefix}encrypted_canonical_payload"
    encrypted_display = f"{prefix}encrypted_display_payload"
    algorithm = f"{prefix}encryption_algorithm"
    key_version = f"{prefix}encryption_key_version"
    canonical_nonce = f"{prefix}canonical_nonce"
    display_nonce = f"{prefix}display_nonce"
    aad = f"{prefix}encryption_aad"
    return CheckConstraint(
        "((sensitivity = 'normal' "
        f"AND {canonical} IS NOT NULL AND {display} IS NOT NULL "
        f"AND length(trim({display})) > 0 "
        f"AND {encrypted_canonical} IS NULL AND {encrypted_display} IS NULL "
        f"AND {algorithm} IS NULL AND {key_version} IS NULL "
        f"AND {canonical_nonce} IS NULL AND {display_nonce} IS NULL AND {aad} IS NULL) "
        "OR (sensitivity = 'sensitive' "
        f"AND {canonical} IS NULL AND {display} IS NULL "
        f"AND {encrypted_canonical} IS NOT NULL AND {encrypted_display} IS NOT NULL "
        f"AND {algorithm} IS NOT NULL AND length(trim({algorithm})) > 0 "
        f"AND {key_version} IS NOT NULL AND length(trim({key_version})) > 0 "
        f"AND {canonical_nonce} IS NOT NULL AND {display_nonce} IS NOT NULL "
        f"AND {aad} IS NOT NULL))",
        name=name,
    )


class MemoryOwnerBindingV2(MemoryV2Base):
    __tablename__ = "memory_owner_bindings_v2"
    __table_args__ = (
        _uuid_check("owner_id", "ck_memory_owner_bindings_v2_owner_uuid"),
        CheckConstraint(
            "length(trim(database_identity)) > 0",
            name="ck_memory_owner_bindings_v2_database_identity",
        ),
        UniqueConstraint("database_identity", name="uq_memory_owner_bindings_v2_database"),
    )

    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    database_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemoryOperationV2(MemoryV2Base):
    __tablename__ = "memory_operations_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_operations_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_operations_v2_owner_uuid"),
        _enum_check(
            "operation_kind",
            [value.value for value in MemoryOperationKind],
            "ck_memory_operations_v2_kind",
        ),
        _enum_check(
            "actor_kind",
            [value.value for value in ActorKind],
            "ck_memory_operations_v2_actor_kind",
        ),
        _enum_check(
            "source_kind",
            [value.value for value in SourceKind],
            "ck_memory_operations_v2_source_kind",
        ),
        _enum_check("status", list(OPERATION_STATUSES), "ck_memory_operations_v2_status"),
        _nullable_enum_check(
            "outcome",
            [value.value for value in MemoryOutcome],
            "ck_memory_operations_v2_outcome",
        ),
        _nullable_enum_check(
            "rejection_code",
            [value.value for value in MemoryRejectionCode],
            "ck_memory_operations_v2_rejection",
        ),
        _nullable_enum_check(
            "error_code",
            [value.value for value in MemoryErrorCode],
            "ck_memory_operations_v2_error",
        ),
        _enum_check(
            "sensitivity",
            [Sensitivity.NORMAL.value, Sensitivity.SENSITIVE.value],
            "ck_memory_operations_v2_sensitivity",
        ),
        CheckConstraint(
            "((sensitivity = 'normal' AND normalized_command_json IS NOT NULL "
            "AND encrypted_command_payload IS NULL AND encryption_algorithm IS NULL "
            "AND encryption_key_version IS NULL AND encryption_nonce IS NULL "
            "AND encryption_aad IS NULL) OR "
            "(sensitivity = 'sensitive' AND normalized_command_json IS NULL "
            "AND encrypted_command_payload IS NOT NULL AND encryption_algorithm IS NOT NULL "
            "AND encryption_key_version IS NOT NULL AND encryption_nonce IS NOT NULL "
            "AND encryption_aad IS NOT NULL))",
            name="ck_memory_operations_v2_payload_shape",
        ),
        CheckConstraint("schema_version > 0", name="ck_memory_operations_v2_schema_version"),
        UniqueConstraint("owner_id", "id", name="uq_memory_operations_v2_owner_id"),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_memory_operations_v2_owner_idempotency"
        ),
        Index("ix_memory_operations_v2_owner_status", "owner_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    normalized_command_json: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(JSON)
    encrypted_command_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_algorithm: Mapped[str | None] = mapped_column(String(80))
    encryption_key_version: Mapped[str | None] = mapped_column(String(80))
    encryption_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_aad: Mapped[bytes | None] = mapped_column(LargeBinary)
    request_hash: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(40))
    rejection_code: Mapped[str | None] = mapped_column(String(80))
    error_code: Mapped[str | None] = mapped_column(String(80))
    result_record_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error_detail: Mapped[str | None] = mapped_column(String(500))
    contract_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=OPERATION_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryMigrationRunV2(MemoryV2Base):
    __tablename__ = "memory_migration_runs_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_migration_runs_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_migration_runs_v2_owner_uuid"),
        _enum_check("phase", list(MIGRATION_RUN_PHASES), "ck_memory_migration_runs_v2_phase"),
        _enum_check("status", list(MIGRATION_RUN_STATUSES), "ck_memory_migration_runs_v2_status"),
        CheckConstraint(
            "source_count >= 0 AND result_count >= 0",
            name="ck_memory_migration_runs_v2_counts",
        ),
        CheckConstraint("schema_version > 0", name="ck_memory_migration_runs_v2_schema_version"),
        UniqueConstraint("owner_id", "id", name="uq_memory_migration_runs_v2_owner_id"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    source_schema_fingerprint: Mapped[str] = mapped_column(
        String(FINGERPRINT_LENGTH), nullable=False
    )
    source_database_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    migration_policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    migration_schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    phase: Mapped[str] = mapped_column(String(24), nullable=False)
    checkpoint_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_checksum: Mapped[str | None] = mapped_column(String(FINGERPRINT_LENGTH))
    result_checksum: Mapped[str | None] = mapped_column(String(FINGERPRINT_LENGTH))
    error_detail: Mapped[str | None] = mapped_column(String(500))
    report_location: Mapped[str | None] = mapped_column(String(512))
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=MIGRATION_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryRecordV2(MemoryV2Base):
    __tablename__ = "memory_records_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_records_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_records_v2_owner_uuid"),
        _enum_check(
            "memory_type",
            [value.value for value in MemoryType],
            "ck_memory_records_v2_type",
        ),
        _enum_check(
            "cardinality",
            [value.value for value in Cardinality],
            "ck_memory_records_v2_cardinality",
        ),
        _enum_check(
            "sensitivity",
            [Sensitivity.NORMAL.value, Sensitivity.SENSITIVE.value],
            "ck_memory_records_v2_sensitivity",
        ),
        _enum_check(
            "status",
            [value.value for value in MemoryLifecycleState],
            "ck_memory_records_v2_status",
        ),
        _payload_shape_check("", "ck_memory_records_v2_payload_shape"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_memory_records_v2_confidence"
        ),
        CheckConstraint(
            "importance >= 1 AND importance <= 10", name="ck_memory_records_v2_importance"
        ),
        CheckConstraint("usage_count >= 0", name="ck_memory_records_v2_usage_count"),
        CheckConstraint("revision > 0", name="ck_memory_records_v2_revision"),
        CheckConstraint(
            "value_schema_version > 0 AND record_schema_version > 0",
            name="ck_memory_records_v2_schema_versions",
        ),
        ForeignKeyConstraint(
            ["owner_id", "created_by_operation_id"],
            ["memory_operations_v2.owner_id", "memory_operations_v2.id"],
            name="fk_memory_records_v2_creating_operation",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_records_v2_owner_id"),
        Index(
            "uq_memory_records_v2_active_exclusive_slot",
            "owner_id",
            "subject_key",
            "memory_type",
            "domain_key",
            "slot_key",
            unique=True,
            sqlite_where=text("status = 'active' AND cardinality = 'exclusive'"),
            postgresql_where=text("status = 'active' AND cardinality = 'exclusive'"),
        ),
        Index(
            "uq_memory_records_v2_active_fingerprint",
            "owner_id",
            "canonical_fingerprint",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_memory_records_v2_owner_status_type", "owner_id", "status", "memory_type"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(40), nullable=False)
    domain_key: Mapped[str] = mapped_column(String(200), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(400), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_payload: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    display_text: Mapped[str | None] = mapped_column(Text)
    encrypted_canonical_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_display_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_algorithm: Mapped[str | None] = mapped_column(String(80))
    encryption_key_version: Mapped[str | None] = mapped_column(String(80))
    canonical_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    display_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_aad: Mapped[bytes | None] = mapped_column(LargeBinary)
    canonical_fingerprint: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_operation_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    contract_version: Mapped[str] = mapped_column(String(80), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    value_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    record_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=RECORD_SCHEMA_VERSION
    )
    legacy_id: Mapped[str | None] = mapped_column(String(160))


class MemoryCandidateV2(MemoryV2Base):
    __tablename__ = "memory_candidates_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_candidates_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_candidates_v2_owner_uuid"),
        _enum_check(
            "memory_type",
            [value.value for value in MemoryType],
            "ck_memory_candidates_v2_type",
        ),
        _enum_check(
            "cardinality",
            [value.value for value in Cardinality],
            "ck_memory_candidates_v2_cardinality",
        ),
        _enum_check(
            "sensitivity",
            [Sensitivity.NORMAL.value, Sensitivity.SENSITIVE.value],
            "ck_memory_candidates_v2_sensitivity",
        ),
        _enum_check(
            "intent",
            [value.value for value in CandidateIntent],
            "ck_memory_candidates_v2_intent",
        ),
        _enum_check(
            "state",
            [value.value for value in CandidateLifecycleState],
            "ck_memory_candidates_v2_state",
        ),
        _nullable_enum_check(
            "decision_outcome",
            [value.value for value in MemoryOutcome],
            "ck_memory_candidates_v2_outcome",
        ),
        _nullable_enum_check(
            "decision_rejection_code",
            [value.value for value in MemoryRejectionCode],
            "ck_memory_candidates_v2_rejection",
        ),
        _nullable_enum_check(
            "decision_error_code",
            [value.value for value in MemoryErrorCode],
            "ck_memory_candidates_v2_error",
        ),
        _payload_shape_check("", "ck_memory_candidates_v2_payload_shape"),
        CheckConstraint(
            "sensitivity = 'normal' OR explicit_user_request = 1",
            name="ck_memory_candidates_v2_sensitive_explicit",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_memory_candidates_v2_confidence"
        ),
        CheckConstraint(
            "importance >= 1 AND importance <= 10", name="ck_memory_candidates_v2_importance"
        ),
        CheckConstraint("revision > 0", name="ck_memory_candidates_v2_revision"),
        CheckConstraint(
            "value_schema_version > 0 AND candidate_schema_version > 0",
            name="ck_memory_candidates_v2_schema_versions",
        ),
        ForeignKeyConstraint(
            ["owner_id", "applied_operation_id"],
            ["memory_operations_v2.owner_id", "memory_operations_v2.id"],
            name="fk_memory_candidates_v2_applied_operation",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_candidates_v2_owner_id"),
        Index("ix_memory_candidates_v2_owner_state", "owner_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(160), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(40), nullable=False)
    domain_key: Mapped[str] = mapped_column(String(200), nullable=False)
    slot_key: Mapped[str] = mapped_column(String(400), nullable=False)
    cardinality: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_payload: Mapped[Any | None] = mapped_column(JSON(none_as_null=True))
    display_text: Mapped[str | None] = mapped_column(Text)
    encrypted_canonical_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encrypted_display_payload: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_algorithm: Mapped[str | None] = mapped_column(String(80))
    encryption_key_version: Mapped[str | None] = mapped_column(String(80))
    canonical_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    display_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    encryption_aad: Mapped[bytes | None] = mapped_column(LargeBinary)
    intent: Mapped[str] = mapped_column(String(24), nullable=False)
    target_hints_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    trusted_target_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    predecessor_evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    source_spans_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    grounding_evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    explicit_user_request: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extractor_name: Mapped[str] = mapped_column(String(120), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(120), nullable=False)
    raw_output_hash: Mapped[str | None] = mapped_column(String(FINGERPRINT_LENGTH))
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_outcome: Mapped[str | None] = mapped_column(String(40))
    decision_rejection_code: Mapped[str | None] = mapped_column(String(80))
    decision_error_code: Mapped[str | None] = mapped_column(String(80))
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    applied_operation_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    contract_version: Mapped[str] = mapped_column(String(80), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    value_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=CANDIDATE_SCHEMA_VERSION
    )


class MemorySourceV2(MemoryV2Base):
    __tablename__ = "memory_sources_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_sources_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_sources_v2_owner_uuid"),
        _enum_check(
            "source_kind",
            [value.value for value in SourceKind],
            "ck_memory_sources_v2_kind",
        ),
        _enum_check(
            "assertion_role",
            list(SOURCE_ASSERTION_ROLES),
            "ck_memory_sources_v2_assertion_role",
        ),
        CheckConstraint(
            "((redacted_excerpt IS NULL AND encrypted_excerpt IS NULL "
            "AND excerpt_encryption_algorithm IS NULL AND excerpt_key_version IS NULL "
            "AND excerpt_nonce IS NULL AND excerpt_aad IS NULL) OR "
            "(redacted_excerpt IS NOT NULL AND encrypted_excerpt IS NULL "
            "AND excerpt_encryption_algorithm IS NULL AND excerpt_key_version IS NULL "
            "AND excerpt_nonce IS NULL AND excerpt_aad IS NULL) OR "
            "(redacted_excerpt IS NULL AND encrypted_excerpt IS NOT NULL "
            "AND excerpt_encryption_algorithm IS NOT NULL AND excerpt_key_version IS NOT NULL "
            "AND excerpt_nonce IS NOT NULL AND excerpt_aad IS NOT NULL))",
            name="ck_memory_sources_v2_excerpt_shape",
        ),
        CheckConstraint("schema_version > 0", name="ck_memory_sources_v2_schema_version"),
        ForeignKeyConstraint(
            ["owner_id", "memory_id"],
            ["memory_records_v2.owner_id", "memory_records_v2.id"],
            name="fk_memory_sources_v2_record",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "operation_id"],
            ["memory_operations_v2.owner_id", "memory_operations_v2.id"],
            name="fk_memory_sources_v2_operation",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_sources_v2_owner_id"),
        UniqueConstraint(
            "owner_id",
            "memory_id",
            "source_content_hash",
            name="uq_memory_sources_v2_record_hash",
        ),
        Index("ix_memory_sources_v2_owner_message", "owner_id", "message_id"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200))
    conversation_id: Mapped[str | None] = mapped_column(String(200))
    session_id: Mapped[str | None] = mapped_column(String(200))
    message_id: Mapped[str | None] = mapped_column(String(200))
    source_span_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    redacted_excerpt: Mapped[str | None] = mapped_column(String(1000))
    encrypted_excerpt: Mapped[bytes | None] = mapped_column(LargeBinary)
    excerpt_encryption_algorithm: Mapped[str | None] = mapped_column(String(80))
    excerpt_key_version: Mapped[str | None] = mapped_column(String(80))
    excerpt_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    excerpt_aad: Mapped[bytes | None] = mapped_column(LargeBinary)
    source_content_hash: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    extractor_version: Mapped[str | None] = mapped_column(String(120))
    assertion_role: Mapped[str] = mapped_column(String(40), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detachment_reason: Mapped[str | None] = mapped_column(String(120))
    operation_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=SOURCE_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemoryRelationV2(MemoryV2Base):
    __tablename__ = "memory_relations_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_relations_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_relations_v2_owner_uuid"),
        _enum_check("relation_type", list(RELATION_TYPES), "ck_memory_relations_v2_relation_type"),
        CheckConstraint("from_memory_id <> to_memory_id", name="ck_memory_relations_v2_not_self"),
        CheckConstraint("schema_version > 0", name="ck_memory_relations_v2_schema_version"),
        ForeignKeyConstraint(
            ["owner_id", "from_memory_id"],
            ["memory_records_v2.owner_id", "memory_records_v2.id"],
            name="fk_memory_relations_v2_from",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "to_memory_id"],
            ["memory_records_v2.owner_id", "memory_records_v2.id"],
            name="fk_memory_relations_v2_to",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["owner_id", "operation_id"],
            ["memory_operations_v2.owner_id", "memory_operations_v2.id"],
            name="fk_memory_relations_v2_operation",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_relations_v2_owner_id"),
        UniqueConstraint(
            "owner_id",
            "from_memory_id",
            "relation_type",
            "to_memory_id",
            name="uq_memory_relations_v2_identity",
        ),
        Index("ix_memory_relations_v2_owner_from", "owner_id", "from_memory_id"),
        Index("ix_memory_relations_v2_owner_to", "owner_id", "to_memory_id"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    from_memory_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    to_memory_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=RELATION_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MemoryOutboxV2(MemoryV2Base):
    __tablename__ = "memory_outbox_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_outbox_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_outbox_v2_owner_uuid"),
        _enum_check("event_kind", list(OUTBOX_EVENT_KINDS), "ck_memory_outbox_v2_event_kind"),
        _enum_check("state", list(OUTBOX_STATES), "ck_memory_outbox_v2_state"),
        CheckConstraint("attempts >= 0", name="ck_memory_outbox_v2_attempts"),
        CheckConstraint(
            "canonical_revision IS NULL OR canonical_revision > 0",
            name="ck_memory_outbox_v2_revision",
        ),
        CheckConstraint(
            "event_kind NOT IN ('canonical_upsert', 'canonical_remove', 'usage') "
            "OR memory_id IS NOT NULL",
            name="ck_memory_outbox_v2_memory_required",
        ),
        CheckConstraint("schema_version > 0", name="ck_memory_outbox_v2_schema_version"),
        ForeignKeyConstraint(
            ["owner_id", "memory_id"],
            ["memory_records_v2.owner_id", "memory_records_v2.id"],
            name="fk_memory_outbox_v2_record",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_outbox_v2_owner_id"),
        UniqueConstraint(
            "owner_id", "event_idempotency_key", name="uq_memory_outbox_v2_idempotency"
        ),
        Index("ix_memory_outbox_v2_owner_state_retry", "owner_id", "state", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    memory_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH))
    canonical_revision: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(FINGERPRINT_LENGTH))
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    event_idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=OUTBOX_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryTombstoneV2(MemoryV2Base):
    __tablename__ = "memory_tombstones_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_tombstones_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_tombstones_v2_owner_uuid"),
        _enum_check(
            "memory_type",
            [value.value for value in MemoryType],
            "ck_memory_tombstones_v2_type",
        ),
        CheckConstraint("expires_at > created_at", name="ck_memory_tombstones_v2_expiration"),
        CheckConstraint("schema_version > 0", name="ck_memory_tombstones_v2_schema_version"),
        ForeignKeyConstraint(
            ["owner_id", "originating_operation_id"],
            ["memory_operations_v2.owner_id", "memory_operations_v2.id"],
            name="fk_memory_tombstones_v2_operation",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_tombstones_v2_owner_id"),
        UniqueConstraint(
            "owner_id",
            "fingerprint_digest",
            "fingerprint_key_version",
            name="uq_memory_tombstones_v2_fingerprint",
        ),
        Index("ix_memory_tombstones_v2_owner_expiry", "owner_id", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    fingerprint_digest: Mapped[str] = mapped_column(String(FINGERPRINT_LENGTH), nullable=False)
    fingerprint_key_version: Mapped[str] = mapped_column(String(80), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(40), nullable=False)
    domain_key: Mapped[str | None] = mapped_column(String(200))
    slot_key: Mapped[str | None] = mapped_column(String(400))
    originating_operation_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    explicitly_reconfirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconfirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=TOMBSTONE_SCHEMA_VERSION
    )


class MemoryLegacyMapV2(MemoryV2Base):
    __tablename__ = "memory_legacy_map_v2"
    __table_args__ = (
        _uuid_check("id", "ck_memory_legacy_map_v2_id_uuid"),
        _uuid_check("owner_id", "ck_memory_legacy_map_v2_owner_uuid"),
        _enum_check(
            "migration_outcome",
            list(LEGACY_MIGRATION_OUTCOMES),
            "ck_memory_legacy_map_v2_outcome",
        ),
        CheckConstraint("schema_version > 0", name="ck_memory_legacy_map_v2_schema_version"),
        ForeignKeyConstraint(
            ["owner_id", "memory_id"],
            ["memory_records_v2.owner_id", "memory_records_v2.id"],
            name="fk_memory_legacy_map_v2_record",
        ),
        ForeignKeyConstraint(
            ["owner_id", "migration_run_id"],
            ["memory_migration_runs_v2.owner_id", "memory_migration_runs_v2.id"],
            name="fk_memory_legacy_map_v2_run",
        ),
        UniqueConstraint("owner_id", "id", name="uq_memory_legacy_map_v2_owner_id"),
        UniqueConstraint(
            "owner_id",
            "source_table",
            "legacy_id",
            name="uq_memory_legacy_map_v2_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(UUID_LENGTH), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(UUID_LENGTH), nullable=False)
    source_table: Mapped[str] = mapped_column(String(160), nullable=False)
    legacy_id: Mapped[str] = mapped_column(String(160), nullable=False)
    memory_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH))
    migration_run_id: Mapped[str | None] = mapped_column(String(UUID_LENGTH))
    migration_outcome: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    outcome_reason: Mapped[str | None] = mapped_column(String(500))
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=MIGRATION_SCHEMA_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
