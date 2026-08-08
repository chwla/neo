"""Versioned contracts for reconstructible Phase 6 derived memory state."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.services.memory.taxonomy import MemoryType
from app.services.memory.versions import (
    DERIVED_DOCUMENT_VERSION,
    EMBEDDING_DOCUMENT_VERSION,
    EMBEDDING_IDENTITY_VERSION,
    OUTBOX_PROCESSING_VERSION,
    RECONCILIATION_POLICY_VERSION,
    RETRY_POLICY_VERSION,
    SEMANTIC_SCORING_VERSION,
    VECTOR_METADATA_VERSION,
)


class MemoryIndexModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DerivedTarget(StrEnum):
    FTS = "fts"
    VECTOR = "vector"


class DerivedTargetState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROCESSING = "processing"
    CURRENT = "current"
    STALE = "stale"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    DELETING = "deleting"
    DELETED = "deleted"


class DerivedMetricCode(StrEnum):
    SEMANTIC_WRONG_OWNER_HIT = "semantic_wrong_owner_hit"
    SEMANTIC_STALE_HIT_DROP = "semantic_stale_hit_drop"
    SEMANTIC_GHOST_HIT_DROP = "semantic_ghost_hit_drop"
    SEMANTIC_INACTIVE_HIT_DROP = "semantic_inactive_hit_drop"


class DerivedFailureCode(StrEnum):
    EMBEDDING_TIMEOUT = "embedding_timeout"
    EMBEDDING_UNAVAILABLE = "embedding_unavailable"
    EMBEDDING_INVALID_RESPONSE = "embedding_invalid_response"
    EMBEDDING_DIMENSION_MISMATCH = "embedding_dimension_mismatch"
    VECTOR_UNAVAILABLE = "vector_unavailable"
    VECTOR_UPSERT_FAILED = "vector_upsert_failed"
    VECTOR_DELETE_FAILED = "vector_delete_failed"
    FTS_UPSERT_FAILED = "fts_upsert_failed"
    FTS_DELETE_FAILED = "fts_delete_failed"
    CANONICAL_MISSING = "canonical_missing"
    CANONICAL_INACTIVE = "canonical_inactive"
    CANONICAL_HASH_ADVANCED = "canonical_hash_advanced"
    OWNER_BINDING_MISMATCH = "owner_binding_mismatch"
    LEASE_LOST = "lease_lost"
    UNKNOWN = "unknown_derived_failure"


class OutboxLease(MemoryIndexModel):
    processing_version: Literal[OUTBOX_PROCESSING_VERSION] = OUTBOX_PROCESSING_VERSION
    event_id: UUID
    owner_id: UUID
    memory_id: UUID | None
    event_kind: str
    targets: tuple[DerivedTarget, ...]
    worker_id: str = Field(min_length=1, max_length=120)
    leased_at: datetime
    lease_expires_at: datetime
    attempt: int = Field(ge=1)


class OutboxBatch(MemoryIndexModel):
    leases: tuple[OutboxLease, ...]


class OutboxTargetDiagnostic(MemoryIndexModel):
    event_id: UUID
    owner_id: UUID
    memory_id: UUID | None
    canonical_revision: int | None
    canonical_content_hash: str | None = Field(max_length=128)
    expected_derived_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    target: DerivedTarget
    operation: Literal["upsert", "delete", "not_applicable"]
    worker_id: str = Field(min_length=1, max_length=120)
    attempt: int = Field(ge=1)
    from_state: Literal["processing"] = "processing"
    to_state: DerivedTargetState
    latency_ms: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    provider_version: str | None = None
    failure_code: DerivedFailureCode | None = None
    repair_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9_:-]+$",
    )


class OutboxProcessResult(MemoryIndexModel):
    event_id: UUID
    completed_targets: tuple[DerivedTarget, ...] = ()
    retryable_targets: tuple[DerivedTarget, ...] = ()
    dead_lettered_targets: tuple[DerivedTarget, ...] = ()
    failure_codes: tuple[DerivedFailureCode, ...] = ()
    diagnostics: tuple[OutboxTargetDiagnostic, ...] = ()
    canonical_mutations: int = 0


class DerivedDocument(MemoryIndexModel):
    schema_version: Literal[DERIVED_DOCUMENT_VERSION] = DERIVED_DOCUMENT_VERSION
    memory_id: UUID
    owner_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_content_hash: str = Field(min_length=1, max_length=128)
    canonical_revision: int = Field(ge=1)
    memory_type: MemoryType
    domain_key: str = Field(min_length=1, max_length=200)
    slot_key: str = Field(min_length=1, max_length=400)
    display_text: str = Field(min_length=1, max_length=12_000)


class EmbeddingDocument(MemoryIndexModel):
    version: Literal[EMBEDDING_DOCUMENT_VERSION] = EMBEDDING_DOCUMENT_VERSION
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=12_000)


class VectorCandidate(MemoryIndexModel):
    owner_id: UUID | None
    memory_id: UUID
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonical_revision: int = Field(ge=1)
    score: float = Field(ge=-1, le=1)
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=160)
    provider_version: str = Field(min_length=1, max_length=80)
    dimension: int = Field(ge=1)
    metadata_version: str = Field(default=VECTOR_METADATA_VERSION, min_length=1, max_length=80)
    derived_schema_version: str = Field(min_length=1, max_length=80)
    embedding_document_version: str = Field(min_length=1, max_length=80)
    embedding_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_identity_version: str = Field(
        default=EMBEDDING_IDENTITY_VERSION,
        min_length=1,
        max_length=80,
    )


class ValidatedSemanticCandidate(MemoryIndexModel):
    memory_id: UUID
    normalized_similarity: float = Field(ge=0, le=1)


class IndexRepairRequest(MemoryIndexModel):
    owner_id: UUID
    memory_id: UUID
    action: Literal["upsert", "delete"]
    target: DerivedTarget | None = None
    reason: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_:-]+$")
    expected_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ReconciliationReport(MemoryIndexModel):
    policy_version: Literal[RECONCILIATION_POLICY_VERSION] = RECONCILIATION_POLICY_VERSION
    owner_id: UUID
    checked: int = Field(ge=0)
    fts_metadata_checked: int = Field(default=0, ge=0)
    vector_metadata_checked: int = Field(default=0, ge=0)
    missing_fts: int = Field(ge=0)
    missing_vector: int = Field(ge=0)
    stale_fts: int = Field(ge=0)
    stale_vector: int = Field(ge=0)
    ghost_fts: int = Field(ge=0)
    ghost_vector: int = Field(ge=0)
    inactive_indexed: int = Field(default=0, ge=0)
    expired_indexed: int = Field(default=0, ge=0)
    policy_ineligible_indexed: int = Field(default=0, ge=0)
    wrong_model_vector: int = Field(default=0, ge=0)
    owner_metadata_mismatch: int = Field(default=0, ge=0)
    pending_already_current: int = Field(default=0, ge=0)
    done_missing_derived: int = Field(default=0, ge=0)
    repairs_queued: int = Field(ge=0)
    dry_run: bool
    checkpoint: str | None = None
    next_checkpoint: str | None = None


class CoverageReport(MemoryIndexModel):
    owner_id: UUID
    canonical_active_eligible_count: int = Field(ge=0)
    fts_current_count: int = Field(ge=0)
    fts_missing_count: int = Field(ge=0)
    fts_stale_count: int = Field(ge=0)
    vector_current_count: int = Field(ge=0)
    vector_missing_count: int = Field(ge=0)
    vector_stale_count: int = Field(ge=0)
    vector_not_applicable_count: int = Field(ge=0)
    pending_outbox_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_pending_age_seconds: int = Field(ge=0)
    maximum_attempts: int = Field(ge=0)
    lease_expired_count: int = Field(ge=0)
    ghost_count: int = Field(ge=0)
    wrong_owner_hit_count: int = Field(ge=0)
    stale_hit_drop_count: int = Field(ge=0)
    provider_healthy: bool
    fts_healthy: bool
    vector_index_healthy: bool
    embedding_provider: str | None = None
    embedding_model: str | None = None
    embedding_provider_version: str | None = None
    embedding_model_coverage_count: int = Field(default=0, ge=0)
    consecutive_provider_failures: int = Field(default=0, ge=0)
    stale_ghost_rate: float = Field(default=0, ge=0, le=1)
    lease_expiration_rate: float = Field(default=0, ge=0, le=1)
    degraded: bool
    ready: bool
    alert_codes: tuple[str, ...] = ()


class RebuildResult(MemoryIndexModel):
    owner_id: UUID
    canonical_checksum_before: str
    canonical_checksum_after: str
    queued: int = Field(ge=0)
    canonical_eligible_count: int = Field(default=0, ge=0)
    fts_cleared_count: int = Field(default=0, ge=0)
    vector_cleared_count: int = Field(default=0, ge=0)
    pending_target_count: int = Field(default=0, ge=0)
    expected_derived_checksum: str = ""
    canonical_mutations: int = 0


class RebuildVerification(MemoryIndexModel):
    owner_id: UUID
    canonical_checksum: str
    expected_derived_checksum: str
    fts_checksum: str
    vector_checksum: str
    canonical_eligible_count: int = Field(ge=0)
    fts_count: int = Field(ge=0)
    vector_count: int = Field(ge=0)
    fts_missing_or_stale: int = Field(ge=0)
    vector_missing_or_stale: int = Field(ge=0)
    equivalent: bool
    canonical_mutations: int = 0


class GlobalCoverageReport(MemoryIndexModel):
    owner_count: int = Field(ge=0)
    canonical_active_eligible_count: int = Field(ge=0)
    fts_current_count: int = Field(ge=0)
    fts_missing_count: int = Field(ge=0)
    fts_stale_count: int = Field(ge=0)
    vector_current_count: int = Field(ge=0)
    vector_missing_count: int = Field(ge=0)
    vector_stale_count: int = Field(ge=0)
    vector_not_applicable_count: int = Field(ge=0)
    pending_outbox_count: int = Field(ge=0)
    processing_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    dead_letter_count: int = Field(ge=0)
    oldest_pending_age_seconds: int = Field(ge=0)
    maximum_attempts: int = Field(ge=0)
    lease_expired_count: int = Field(ge=0)
    ghost_count: int = Field(ge=0)
    wrong_owner_hit_count: int = Field(ge=0)
    stale_hit_drop_count: int = Field(ge=0)
    embedding_model_coverage_count: int = Field(ge=0)
    unhealthy_provider_owner_count: int = Field(ge=0)
    unhealthy_fts_owner_count: int = Field(ge=0)
    unhealthy_vector_owner_count: int = Field(ge=0)
    maximum_consecutive_provider_failures: int = Field(ge=0)
    stale_ghost_rate: float = Field(ge=0, le=1)
    lease_expiration_rate: float = Field(ge=0, le=1)
    degraded_owner_count: int = Field(ge=0)
    ready: bool
    alert_codes: tuple[str, ...] = ()


class ProviderHealth(MemoryIndexModel):
    provider: str
    model: str
    provider_version: str
    healthy: bool
    failure_code: str | None = None


class SemanticRecallDiagnostic(MemoryIndexModel):
    scoring_policy_version: Literal[SEMANTIC_SCORING_VERSION] = SEMANTIC_SCORING_VERSION
    semantic_candidate_count: int = Field(ge=0)
    canonical_validated_count: int = Field(ge=0)
    stale_drop_count: int = Field(ge=0)
    ghost_drop_count: int = Field(ge=0)
    wrong_owner_drop_count: int = Field(ge=0)
    inactive_drop_count: int = Field(ge=0)
    repair_count: int = Field(ge=0)
    degraded_reason: str | None = None


class RetryPolicy(MemoryIndexModel):
    version: Literal[RETRY_POLICY_VERSION] = RETRY_POLICY_VERSION
    maximum_attempts: int = Field(default=5, ge=1, le=100)
    dead_letter_threshold: int | None = Field(default=None, ge=1, le=100)
    base_delay_seconds: int = Field(default=5, ge=1, le=3_600)
    maximum_delay_seconds: int = Field(default=300, ge=1, le=86_400)
    jitter_seconds: int = Field(default=0, ge=0, le=3_600)
    lease_seconds: int = Field(default=60, ge=5, le=3_600)
    batch_size: int = Field(default=25, ge=1, le=500)

    @property
    def dead_letter_after(self) -> int:
        return min(self.maximum_attempts, self.dead_letter_threshold or self.maximum_attempts)

    def delay_for(self, attempt: int, *, jitter_fraction: float = 0) -> int:
        bounded_jitter = min(1.0, max(0.0, jitter_fraction))
        exponential = self.base_delay_seconds * 2 ** max(0, attempt - 1)
        return min(
            self.maximum_delay_seconds,
            exponential + int(self.jitter_seconds * bounded_jitter),
        )
