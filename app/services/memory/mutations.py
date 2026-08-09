"""Sole transactional coordinator for the isolated memory Phase 2 kernel."""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import sleep
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.identifiers import canonical_uuid
from app.models.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryOutbox,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryTombstone,
)
from app.repositories.memory import (
    MemoryNotFoundError,
    MemoryRepository,
    MemoryRevisionConflict,
)
from app.services.memory.contracts import (
    MEMORY_COMMAND_ADAPTER,
    CandidateLifecycleState,
    CandidatePersistenceOutcome,
    CandidatePersistenceResult,
    CandidateStatusSnapshot,
    CanonicalMemorySnapshot,
    CreateMemoryCommand,
    DerivedState,
    DetachMemorySourceCommand,
    EvidenceRole,
    ForgetMemoryCommand,
    MemoryCommand,
    MemoryCommandBase,
    MemoryCommandResult,
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MergeMemoryCommand,
    PersistExtractionCandidateCommand,
    ReplaceMemoryCommand,
    RestoreMemoryCommand,
    Sensitivity,
    SourceChangeOutcome,
    SourceChangeResult,
    UpdateMemoryCommand,
    ValidatedCandidateProposal,
)
from app.services.memory.crypto import (
    EncryptedValue,
    KeyedFingerprintProvider,
    KeyVersionResolver,
    SensitivePayloadProvider,
    TombstoneHMACProvider,
    build_associated_data,
)
from app.services.memory.normalization import (
    MemoryNormalizationError,
    NormalizedCandidate,
    NormalizedRecordValue,
    canonical_json_bytes,
    normalize_candidate,
    normalize_record_value,
    normalize_source,
    operation_request_hash,
    validate_command_versions,
)
from app.services.memory.planner import (
    CandidateSnapshot,
    MutationPlan,
    PlannerContext,
    PlannerState,
    RecordSnapshot,
    RelationSnapshot,
    SourceCreateSpec,
    plan_memory_mutation,
)
from app.services.memory.policy import candidate_persistence_decision, classify_sensitivity
from app.services.memory.taxonomy import Cardinality, MemoryType
from app.services.memory.tombstones import (
    TombstoneSnapshot,
    resurrection_blocked,
    tombstone_digest,
    tombstone_matches,
)
from app.services.memory.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION

FailureInjector = Callable[[str], None]
Clock = Callable[[], datetime]

_REPLAY_ENVELOPE_PREFIX = "replay_v1:"


class MemoryMutationError(RuntimeError):
    pass


class InjectedMutationFailure(MemoryMutationError):
    pass


class _PlanChanged(RuntimeError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    # A mutation plans outside the short writer transaction, then verifies that
    # snapshot under BEGIN IMMEDIATE.  Several independent additive writes can
    # therefore legitimately need to re-plan behind one another.  Four 10ms
    # attempts regularly expires under a normal browser burst.
    attempts: int = 10
    base_delay_seconds: float = 0.02

    def __post_init__(self) -> None:
        if not 1 <= self.attempts <= 10:
            raise ValueError("retry_attempts_out_of_range")
        if not 0 <= self.base_delay_seconds <= 1:
            raise ValueError("retry_delay_out_of_range")

    def delay_for_attempt(self, attempt: int) -> float:
        """Decorrelate competing SQLite writers before they re-plan."""

        return self.base_delay_seconds * (attempt + 1) * random.uniform(0.5, 1.5)


@dataclass(frozen=True)
class _PreparedMutation:
    plan: MutationPlan
    state: PlannerState
    context: PlannerContext
    request_hash: str
    effective_sensitivity: Sensitivity
    normalized_command_json: dict[str, Any] | None
    encrypted_command: EncryptedValue | None
    crypto_material: dict[str, dict[str, Any]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _effective_command_sensitivity(command: MemoryCommandBase) -> Sensitivity:
    declared = Sensitivity.NORMAL
    candidate = _candidate_for(command)
    material: list[str] = []
    if candidate is not None:
        declared = candidate.sensitivity
        material.extend(
            (
                json.dumps(candidate.canonical_value, sort_keys=True),
                candidate.display_text,
                *(item.text for item in candidate.evidence),
            )
        )
    elif isinstance(command, UpdateMemoryCommand) and command.patch.sensitivity is not None:
        declared = command.patch.sensitivity
        material.extend(
            (
                json.dumps(command.patch.canonical_value, sort_keys=True),
                command.patch.display_text or "",
            )
        )
    material.extend(item.text for item in command.source.evidence)
    detected = classify_sensitivity("\n".join(material))
    order = {Sensitivity.NORMAL: 0, Sensitivity.SENSITIVE: 1, Sensitivity.PROHIBITED: 2}
    return max((declared, detected), key=order.__getitem__)


def _candidate_for(command: MemoryCommandBase) -> ValidatedCandidateProposal | None:
    if isinstance(command, (CreateMemoryCommand, ReplaceMemoryCommand, MergeMemoryCommand)):
        return command.candidate
    if isinstance(command, RestoreMemoryCommand):
        return command.replacement_candidate
    return None


def _redacted_command(command: MemoryCommandBase) -> dict[str, Any]:
    return {
        "contract_version": command.contract_version,
        "idempotency_key": command.idempotency_key,
        "operation": command.operation.value,
        "owner_id": command.owner_id,
        "policy_version": command.policy_version,
        "redacted": True,
        "taxonomy_version": command.taxonomy_version,
    }


def _fixed_failed_result(
    command: MemoryCommandBase,
    operation_id: str,
    error_code: MemoryErrorCode,
    message: str,
) -> MemoryCommandResult:
    return MemoryCommandResult(
        operation_id=UUID(operation_id),
        owner_id=command.owner_id,
        operation=command.operation,
        outcome=MemoryOutcome.FAILED,
        error_code=error_code,
        message=message,
    )


def _encode_replay_envelope(result: MemoryCommandResult) -> str:
    """Persist the result fields not represented by Phase 1 ledger columns.

    The Phase 1 schema is authoritative and has no dedicated result snapshot JSON
    column.  This compact envelope uses its bounded, non-sensitive diagnostic field
    so a replay does not accidentally report a record's *later* revision.
    """

    payload = {
        "a": [str(item) for item in result.active_memory_ids],
        "c": str(result.candidate_id) if result.candidate_id else None,
        "d": result.derived_state.value,
        "m": result.message,
        "r": result.current_revision,
    }
    encoded = _REPLAY_ENVELOPE_PREFIX + json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) > 500:
        raise MemoryMutationError("replay_envelope_exceeds_ledger_bound")
    return encoded


def _decode_replay_envelope(value: str | None) -> dict[str, Any] | None:
    if not value or not value.startswith(_REPLAY_ENVELOPE_PREFIX):
        return None
    try:
        decoded = json.loads(value[len(_REPLAY_ENVELOPE_PREFIX) :])
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


class MemoryMutationService:
    """Execute validated commands atomically against one explicitly bound profile DB."""

    def __init__(
        self,
        engine: Engine,
        *,
        owner_id: str,
        database_identity: str,
        payload_provider: SensitivePayloadProvider,
        fingerprint_provider: KeyedFingerprintProvider,
        tombstone_provider: TombstoneHMACProvider,
        key_versions: KeyVersionResolver,
        retry_policy: RetryPolicy | None = None,
        failure_injector: FailureInjector | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("phase2_mutation_kernel_requires_sqlite")
        self._engine = engine
        self.owner_id = canonical_uuid(owner_id)
        if not database_identity.strip():
            raise ValueError("database_identity_required")
        self.database_identity = database_identity
        self._payload_provider = payload_provider
        self._fingerprint_provider = fingerprint_provider
        self._tombstone_provider = tombstone_provider
        self._key_versions = key_versions
        self._retry = retry_policy or RetryPolicy()
        self._failure_injector = failure_injector
        self._clock = clock
        self._sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def execute(self, command: MemoryCommand | dict[str, Any]) -> MemoryCommandResult:
        try:
            validated = (
                MEMORY_COMMAND_ADAPTER.validate_python(command)
                if isinstance(command, dict)
                else command
            )
        except ValidationError:
            # Pydantic's detailed rendering includes rejected input values. Keep the
            # isolated kernel boundary fixed and non-echoing instead.
            raise MemoryMutationError("invalid_command_shape") from None
        operation_id = str(uuid4())
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if validated.owner_id != self.owner_id:
            return _fixed_failed_result(
                validated,
                operation_id,
                MemoryErrorCode.OWNER_MISMATCH,
                "owner_mismatch",
            )
        try:
            validate_command_versions(validated)
        except MemoryNormalizationError as exc:
            return _fixed_failed_result(
                validated,
                operation_id,
                self._normalization_error_code(exc.code),
                exc.code,
            )

        sensitivity = _effective_command_sensitivity(validated)
        try:
            request_hash = operation_request_hash(
                validated,
                keyed_provider=self._fingerprint_provider,
                sensitivity=sensitivity,
            )
        except Exception:
            return _fixed_failed_result(
                validated,
                operation_id,
                MemoryErrorCode.INTERNAL_ERROR,
                "secure_request_fingerprint_unavailable",
            )

        replay = self._preflight_idempotency(validated, request_hash)
        if replay is not None:
            return replay

        if validated.dry_run and sensitivity is not Sensitivity.PROHIBITED:
            try:
                prepared = self._prepare(
                    validated,
                    operation_id=operation_id,
                    request_hash=request_hash,
                    effective_sensitivity=sensitivity,
                    now=now,
                )
            except (MemoryNormalizationError, MemoryNotFoundError) as exc:
                return _fixed_failed_result(
                    validated,
                    operation_id,
                    self._normalization_error_code(str(exc)),
                    str(exc),
                )
            return self._result_from_plan(validated, prepared.plan)

        if sensitivity is Sensitivity.PROHIBITED:
            return self._persist_prohibited_rejection(
                validated,
                operation_id=operation_id,
                request_hash=request_hash,
                now=now,
            )

        for attempt in range(self._retry.attempts):
            try:
                prepared = self._prepare(
                    validated,
                    operation_id=operation_id,
                    request_hash=request_hash,
                    effective_sensitivity=sensitivity,
                    now=now,
                )
                return self._commit_prepared(validated, prepared)
            except (_PlanChanged, IntegrityError, MemoryRevisionConflict):
                replay = self._preflight_idempotency(validated, request_hash)
                if replay is not None:
                    return replay
                if attempt + 1 >= self._retry.attempts:
                    return _fixed_failed_result(
                        validated,
                        operation_id,
                        MemoryErrorCode.REVISION_CONFLICT,
                        "concurrent_mutation_conflict",
                    )
            except OperationalError as exc:
                if not self._is_sqlite_busy(exc) or attempt + 1 >= self._retry.attempts:
                    return _fixed_failed_result(
                        validated,
                        operation_id,
                        MemoryErrorCode.INTERNAL_ERROR,
                        "database_temporarily_unavailable",
                    )
            except (MemoryNormalizationError, MemoryNotFoundError) as exc:
                return _fixed_failed_result(
                    validated,
                    operation_id,
                    self._normalization_error_code(str(exc)),
                    str(exc),
                )
            except InjectedMutationFailure:
                return _fixed_failed_result(
                    validated,
                    operation_id,
                    MemoryErrorCode.INTERNAL_ERROR,
                    "injected_mutation_failure",
                )
            except SQLAlchemyError:
                return _fixed_failed_result(
                    validated,
                    operation_id,
                    MemoryErrorCode.INTERNAL_ERROR,
                    "canonical_mutation_failed",
                )
            if self._retry.base_delay_seconds:
                sleep(self._retry.delay_for_attempt(attempt))
        return _fixed_failed_result(
            validated,
            operation_id,
            MemoryErrorCode.INTERNAL_ERROR,
            "canonical_mutation_failed",
        )

    def detach_source(self, command: DetachMemorySourceCommand) -> SourceChangeResult:
        """Persist one provenance detachment without a canonical operation or revision bump."""

        if command.owner_id != self.owner_id:
            return self._source_change_result(
                command,
                SourceChangeOutcome.OWNER_MISMATCH,
                reason="owner_mismatch",
            )

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with self._sessions.begin() as session:
            source = session.scalar(
                select(MemorySource).where(MemorySource.id == str(command.source_id))
            )
            if source is None:
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.SOURCE_NOT_FOUND,
                    reason="source_not_found",
                )
            if source.owner_id != self.owner_id:
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.OWNER_MISMATCH,
                    reason="owner_mismatch",
                )
            if source.memory_id != str(command.target.memory_id):
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.SOURCE_NOT_FOUND,
                    reason="source_memory_mismatch",
                )

            record = session.scalar(
                select(MemoryRecord).where(
                    MemoryRecord.owner_id == self.owner_id,
                    MemoryRecord.id == str(command.target.memory_id),
                )
            )
            if record is None:
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.SOURCE_NOT_FOUND,
                    reason="memory_not_found",
                )
            if record.revision != command.target.expected_revision:
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.REVISION_CONFLICT,
                    memory_revision=record.revision,
                    reason="revision_conflict",
                )

            remaining = session.scalar(
                select(func.count(MemorySource.id)).where(
                    MemorySource.owner_id == self.owner_id,
                    MemorySource.memory_id == record.id,
                    MemorySource.assertion_role == "supports",
                    MemorySource.is_active.is_(True),
                )
            )
            active_support_count = int(remaining or 0)
            if not source.is_active:
                return self._source_change_result(
                    command,
                    SourceChangeOutcome.ALREADY_DETACHED,
                    detached_source_id=command.source_id,
                    remaining_active_source_count=active_support_count,
                    memory_revision=record.revision,
                    review_required=active_support_count == 0,
                    reason="source_already_detached",
                )

            source.is_active = False
            source.detachment_reason = command.detachment_reason
            source.updated_at = now
            session.flush()
            if source.assertion_role == "supports":
                active_support_count -= 1
            outcome = (
                SourceChangeOutcome.PRESERVED
                if active_support_count > 0
                else SourceChangeOutcome.NEEDS_REVIEW
            )
            return self._source_change_result(
                command,
                outcome,
                detached_source_id=command.source_id,
                remaining_active_source_count=active_support_count,
                memory_revision=record.revision,
                review_required=active_support_count == 0,
                reason=(
                    "source_detached_memory_preserved"
                    if active_support_count > 0
                    else "final_source_detached_review_required"
                ),
            )

    def list_active_records(
        self,
        *,
        limit: int = 200,
        include_archived: bool = False,
    ) -> tuple[CanonicalMemorySnapshot, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("record_limit_out_of_range")
        with self._sessions() as session:
            MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            state = self._load_state(session)
        allowed_statuses = {MemoryLifecycleState.ACTIVE}
        if include_archived:
            allowed_statuses.add(MemoryLifecycleState.ARCHIVED)
        active = [
            item
            for item in state.records
            if item.status in allowed_statuses
            and item.canonical_value is not None
            and item.display_text is not None
        ]
        active.sort(key=lambda item: item.id)
        return tuple(
            CanonicalMemorySnapshot(
                memory_id=UUID(item.id),
                owner_id=item.owner_id,
                subject_key=item.subject_key,
                memory_type=item.memory_type,
                scope_type=item.scope_type,
                scope_project_id=item.scope_project_id,
                domain_key=item.domain_key,
                slot_key=item.slot_key,
                cardinality=item.cardinality,
                canonical_value=item.canonical_value,
                display_text=item.display_text,
                sensitivity=Sensitivity(item.sensitivity),
                status=item.status,
                revision=item.revision,
            )
            for item in active[:limit]
        )

    def candidate_status(self, candidate_id: UUID) -> CandidateStatusSnapshot | None:
        with self._sessions() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            candidate = repository.get_candidate(str(candidate_id))
            if candidate is None:
                return None
            return CandidateStatusSnapshot(
                owner_id=self.owner_id,
                candidate_id=UUID(candidate.id),
                state=CandidateLifecycleState(candidate.state),
                revision=candidate.revision,
                decision_outcome=(
                    MemoryOutcome(candidate.decision_outcome)
                    if candidate.decision_outcome
                    else None
                ),
                rejection_code=(
                    MemoryRejectionCode(candidate.decision_rejection_code)
                    if candidate.decision_rejection_code
                    else None
                ),
                applied_operation_id=(
                    UUID(candidate.applied_operation_id) if candidate.applied_operation_id else None
                ),
            )

    def reject_candidate(
        self, candidate_id: UUID, *, expected_revision: int
    ) -> CandidateStatusSnapshot:
        with self._sessions.begin() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            candidate = repository.get_candidate(str(candidate_id))
            if candidate is None:
                raise MemoryNotFoundError("candidate_not_found")
            if candidate.state in {
                CandidateLifecycleState.REJECTED.value,
                CandidateLifecycleState.APPLIED.value,
            }:
                return CandidateStatusSnapshot(
                    owner_id=self.owner_id,
                    candidate_id=UUID(candidate.id),
                    state=CandidateLifecycleState(candidate.state),
                    revision=candidate.revision,
                    decision_outcome=(
                        MemoryOutcome(candidate.decision_outcome)
                        if candidate.decision_outcome
                        else None
                    ),
                    rejection_code=(
                        MemoryRejectionCode(candidate.decision_rejection_code)
                        if candidate.decision_rejection_code
                        else None
                    ),
                    applied_operation_id=(
                        UUID(candidate.applied_operation_id)
                        if candidate.applied_operation_id
                        else None
                    ),
                )
            candidate = repository.update_candidate_decision(
                str(candidate_id),
                expected_revision=expected_revision,
                values={
                    "state": CandidateLifecycleState.REJECTED.value,
                    "decision_outcome": MemoryOutcome.REJECTED.value,
                    "decision_rejection_code": MemoryRejectionCode.USER_REJECTED.value,
                    "decision_reason": "user_rejected_candidate",
                    "decided_at": self._clock(),
                },
            )
            return CandidateStatusSnapshot(
                owner_id=self.owner_id,
                candidate_id=UUID(candidate.id),
                state=CandidateLifecycleState(candidate.state),
                revision=candidate.revision,
                decision_outcome=MemoryOutcome(candidate.decision_outcome),
                rejection_code=MemoryRejectionCode(candidate.decision_rejection_code),
            )

    def persist_extraction_candidate(
        self,
        command: PersistExtractionCandidateCommand,
    ) -> CandidatePersistenceResult:
        if command.owner_id != self.owner_id:
            return CandidatePersistenceResult(
                outcome=CandidatePersistenceOutcome.REJECTED,
                owner_id=command.owner_id,
                candidate_id=command.candidate.proposal_id,
                reason="owner_mismatch",
            )
        policy = candidate_persistence_decision(command.candidate)
        if not policy.allowed:
            return CandidatePersistenceResult(
                outcome=(
                    CandidatePersistenceOutcome.PROHIBITED
                    if policy.sensitivity is Sensitivity.PROHIBITED
                    else CandidatePersistenceOutcome.REJECTED
                ),
                owner_id=self.owner_id,
                candidate_id=command.candidate.proposal_id,
                reason=(
                    policy.rejection_code.value
                    if policy.rejection_code
                    else "candidate_policy_rejected"
                ),
            )
        candidate_contract = command.candidate.model_copy(
            update={"sensitivity": policy.sensitivity}
        )
        if policy.sensitivity is Sensitivity.SENSITIVE:
            safe_target_hints = candidate_contract.target_hints.model_copy(
                update={"old_value_phrases": ()}
            )
            candidate_contract = candidate_contract.model_copy(
                update={"target_hints": safe_target_hints}
            )
        try:
            normalized = normalize_candidate(
                candidate_contract,
                owner_id=self.owner_id,
                keyed_provider=self._fingerprint_provider,
            )
        except MemoryNormalizationError as exc:
            return CandidatePersistenceResult(
                outcome=CandidatePersistenceOutcome.REJECTED,
                owner_id=self.owner_id,
                candidate_id=command.candidate.proposal_id,
                reason=exc.code,
            )
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with self._sessions.begin() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            existing = repository.get_candidate(normalized.candidate_id)
            if existing is not None:
                return CandidatePersistenceResult(
                    outcome=CandidatePersistenceOutcome.ALREADY_EXISTS,
                    owner_id=self.owner_id,
                    candidate_id=UUID(existing.id),
                    state=CandidateLifecycleState(existing.state),
                    revision=existing.revision,
                    applied_operation_id=(
                        UUID(existing.applied_operation_id)
                        if existing.applied_operation_id
                        else None
                    ),
                    reason="candidate_already_exists",
                )
            payload = (
                self._encrypted_candidate_payload(normalized.candidate_id, normalized)
                if normalized.sensitivity is Sensitivity.SENSITIVE
                else {
                    "canonical_payload": normalized.canonical_value,
                    "display_text": normalized.display_text,
                    "encrypted_canonical_payload": None,
                    "encrypted_display_payload": None,
                    "encryption_algorithm": None,
                    "encryption_key_version": None,
                    "canonical_nonce": None,
                    "display_nonce": None,
                    "encryption_aad": None,
                }
            )
            repository.add_candidate(
                MemoryCandidate(
                    id=normalized.candidate_id,
                    owner_id=self.owner_id,
                    subject_key=normalized.subject_key,
                    memory_type=normalized.memory_type.value,
                    scope_type=normalized.scope_type,
                    scope_project_id=normalized.scope_project_id,
                    domain_key=normalized.domain_key,
                    slot_key=normalized.slot_key,
                    cardinality=normalized.cardinality.value,
                    sensitivity=normalized.sensitivity.value,
                    **payload,
                    intent=normalized.intent.value,
                    target_hints_json=normalized.target_hints,
                    trusted_target_ids=list(normalized.target_ids),
                    predecessor_evidence_json=dict(command.predecessor_evidence),
                    source_spans_json=[
                        item.model_dump(mode="json") for item in command.source_spans
                    ],
                    grounding_evidence_json=dict(command.grounding_evidence),
                    confidence=normalized.confidence,
                    importance=normalized.importance,
                    explicit_user_request=normalized.explicit_user_request,
                    extractor_name=command.extractor_name,
                    extractor_version=command.extractor_version,
                    raw_output_hash=command.raw_output_hash,
                    state=command.state.value,
                    decision_outcome=(
                        command.decision_outcome.value if command.decision_outcome else None
                    ),
                    decision_rejection_code=(
                        command.rejection_code.value if command.rejection_code else None
                    ),
                    decision_error_code=None,
                    decision_reason=command.decision_reason,
                    applied_operation_id=None,
                    created_at=now,
                    decided_at=(
                        now if command.state is CandidateLifecycleState.NEEDS_REVIEW else None
                    ),
                    revision=1,
                    contract_version=CONTRACT_VERSION,
                    policy_version=POLICY_VERSION,
                    taxonomy_version=TAXONOMY_VERSION,
                    value_schema_version=normalized.value_schema_version,
                    candidate_schema_version=1,
                )
            )
            return CandidatePersistenceResult(
                outcome=CandidatePersistenceOutcome.PERSISTED,
                owner_id=self.owner_id,
                candidate_id=UUID(normalized.candidate_id),
                state=command.state,
                revision=1,
                reason="candidate_persisted",
            )

    @staticmethod
    def _source_change_result(
        command: DetachMemorySourceCommand,
        outcome: SourceChangeOutcome,
        *,
        detached_source_id: UUID | None = None,
        remaining_active_source_count: int | None = None,
        memory_revision: int | None = None,
        review_required: bool = False,
        reason: str,
    ) -> SourceChangeResult:
        return SourceChangeResult(
            outcome=outcome,
            owner_id=command.owner_id,
            memory_id=command.target.memory_id,
            requested_source_id=command.source_id,
            detached_source_id=detached_source_id,
            remaining_active_source_count=remaining_active_source_count,
            memory_revision=memory_revision,
            review_required=review_required,
            idempotency_key=command.idempotency_key,
            reason=reason,
        )

    @staticmethod
    def _normalization_error_code(code: str) -> MemoryErrorCode:
        mapping = {
            "unsupported_contract_version": MemoryErrorCode.UNSUPPORTED_CONTRACT_VERSION,
            "unsupported_policy_version": MemoryErrorCode.UNSUPPORTED_POLICY_VERSION,
            "unsupported_taxonomy_version": MemoryErrorCode.UNSUPPORTED_TAXONOMY_VERSION,
            "revision_conflict": MemoryErrorCode.REVISION_CONFLICT,
            "memory_not_found": MemoryErrorCode.NOT_FOUND,
        }
        return mapping.get(code, MemoryErrorCode.INVALID_COMMAND)

    @staticmethod
    def _is_sqlite_busy(exc: OperationalError) -> bool:
        original = exc.orig
        if isinstance(original, sqlite3.OperationalError):
            lowered = str(original).casefold()
            return "locked" in lowered or "busy" in lowered
        return False

    def _preflight_idempotency(
        self,
        command: MemoryCommandBase,
        request_hash: str,
    ) -> MemoryCommandResult | None:
        with self._sessions() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            operation = repository.get_operation_by_idempotency_key(command.idempotency_key)
            if operation is None:
                return None
            if operation.request_hash != request_hash:
                return _fixed_failed_result(
                    command,
                    operation.id,
                    MemoryErrorCode.IDEMPOTENCY_CONFLICT,
                    "idempotency_key_reused_with_different_request",
                )
            if operation.status in {"committed", "rejected", "failed"}:
                return self._result_from_operation(session, operation)
            return None

    def _persist_prohibited_rejection(
        self,
        command: MemoryCommand,
        *,
        operation_id: str,
        request_hash: str,
        now: datetime,
    ) -> MemoryCommandResult:
        for attempt in range(self._retry.attempts):
            session = self._sessions()
            try:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                repository = MemoryRepository(
                    session,
                    owner_id=self.owner_id,
                    database_identity=self.database_identity,
                )
                existing = repository.get_operation_by_idempotency_key(command.idempotency_key)
                if existing is not None:
                    session.rollback()
                    if existing.request_hash == request_hash:
                        return self._result_from_operation(session, existing)
                    return _fixed_failed_result(
                        command,
                        existing.id,
                        MemoryErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency_key_reused_with_different_request",
                    )
                operation = self._operation_row(
                    command,
                    operation_id=operation_id,
                    request_hash=request_hash,
                    sensitivity=Sensitivity.NORMAL,
                    normalized_json=_redacted_command(command),
                    encrypted=None,
                    now=now,
                )
                operation.status = "rejected"
                operation.outcome = MemoryOutcome.REJECTED.value
                operation.rejection_code = MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT.value
                rejected_result = MemoryCommandResult(
                    operation_id=UUID(operation.id),
                    owner_id=command.owner_id,
                    operation=command.operation,
                    outcome=MemoryOutcome.REJECTED,
                    rejection_code=MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT,
                    message="prohibited_content_not_persisted",
                )
                operation.error_detail = _encode_replay_envelope(rejected_result)
                repository.add_operation(operation)
                session.commit()
                return self._result_from_operation(session, operation)
            except (IntegrityError, OperationalError) as exc:
                session.rollback()
                if isinstance(exc, OperationalError) and not self._is_sqlite_busy(exc):
                    break
                replay = self._preflight_idempotency(command, request_hash)
                if replay is not None:
                    return replay
                if attempt + 1 < self._retry.attempts and self._retry.base_delay_seconds:
                    sleep(self._retry.delay_for_attempt(attempt))
            finally:
                session.close()
        return _fixed_failed_result(
            command,
            operation_id,
            MemoryErrorCode.INTERNAL_ERROR,
            "prohibited_rejection_ledger_unavailable",
        )

    def _prepare(
        self,
        command: MemoryCommand,
        *,
        operation_id: str,
        request_hash: str,
        effective_sensitivity: Sensitivity,
        now: datetime,
    ) -> _PreparedMutation:
        with self._sessions() as session:
            MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            state = self._load_state(session)
            context = self._build_context(command, state, operation_id=operation_id, now=now)
            plan = plan_memory_mutation(command, state, context)

        normalized_json: dict[str, Any] | None
        encrypted_command: EncryptedValue | None
        if effective_sensitivity is Sensitivity.SENSITIVE:
            normalized_json = None
            encrypted_command = self._encrypt_command(command, context)
        else:
            normalized_json = command.model_dump(mode="json", exclude_none=False)
            encrypted_command = None

        crypto_material = self._precompute_plan_crypto(command, state, context, plan)
        return _PreparedMutation(
            plan=plan,
            state=state,
            context=context,
            request_hash=request_hash,
            effective_sensitivity=effective_sensitivity,
            normalized_command_json=normalized_json,
            encrypted_command=encrypted_command,
            crypto_material=crypto_material,
        )

    def _load_state(self, session: Session, *, decrypt_sensitive: bool = True) -> PlannerState:
        records = tuple(
            self._record_snapshot(item, decrypt_sensitive=decrypt_sensitive)
            for item in session.scalars(
                select(MemoryRecord).where(MemoryRecord.owner_id == self.owner_id)
            )
        )
        candidates = tuple(
            CandidateSnapshot(item.id, CandidateLifecycleState(item.state))
            for item in session.scalars(
                select(MemoryCandidate).where(MemoryCandidate.owner_id == self.owner_id)
            )
        )
        relations = tuple(
            RelationSnapshot(item.from_memory_id, item.relation_type, item.to_memory_id)
            for item in session.scalars(
                select(MemoryRelation).where(MemoryRelation.owner_id == self.owner_id)
            )
        )
        tombstones = tuple(
            TombstoneSnapshot(
                id=item.id,
                owner_id=item.owner_id,
                fingerprint_digest=item.fingerprint_digest,
                fingerprint_key_version=item.fingerprint_key_version,
                memory_type=item.memory_type,
                domain_key=item.domain_key,
                slot_key=item.slot_key,
                created_at=self._aware(item.created_at),
                expires_at=self._aware(item.expires_at),
                explicitly_reconfirmed=item.explicitly_reconfirmed,
            )
            for item in session.scalars(
                select(MemoryTombstone).where(MemoryTombstone.owner_id == self.owner_id)
            )
        )
        return PlannerState(
            owner_id=self.owner_id,
            records=records,
            candidates=candidates,
            relations=relations,
            tombstones=tombstones,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _record_snapshot(
        self,
        item: MemoryRecord,
        *,
        decrypt_sensitive: bool,
    ) -> RecordSnapshot:
        canonical: Any = item.canonical_payload
        display = item.display_text
        if item.sensitivity == Sensitivity.SENSITIVE.value and decrypt_sensitive:
            canonical, display = self._decrypt_record(item)
        return RecordSnapshot(
            id=item.id,
            owner_id=item.owner_id,
            subject_key=item.subject_key,
            memory_type=MemoryType(item.memory_type),
            scope_type=item.scope_type,
            scope_project_id=item.scope_project_id,
            domain_key=item.domain_key,
            slot_key=item.slot_key,
            cardinality=Cardinality(item.cardinality),
            sensitivity=item.sensitivity,
            canonical_value=canonical,
            display_text=display,
            canonical_fingerprint=item.canonical_fingerprint,
            confidence=item.confidence,
            importance=item.importance,
            status=MemoryLifecycleState(item.status),
            revision=item.revision,
            usage_count=item.usage_count,
            pinned=item.pinned,
            metadata=dict(item.metadata_json or {}),
            value_schema_version=item.value_schema_version,
        )

    def _decrypt_record(self, item: MemoryRecord) -> tuple[Any, str]:
        if not all(
            (
                item.encrypted_canonical_payload,
                item.encrypted_display_payload,
                item.encryption_algorithm,
                item.encryption_key_version,
                item.canonical_nonce,
                item.display_nonce,
                item.encryption_aad,
            )
        ):
            raise MemoryMutationError("invalid_sensitive_record_shape")
        canonical_payload = EncryptedValue(
            ciphertext=item.encrypted_canonical_payload,
            algorithm=item.encryption_algorithm,
            key_version=item.encryption_key_version,
            nonce=item.canonical_nonce,
            associated_data=item.encryption_aad,
        )
        display_payload = EncryptedValue(
            ciphertext=item.encrypted_display_payload,
            algorithm=item.encryption_algorithm,
            key_version=item.encryption_key_version,
            nonce=item.display_nonce,
            associated_data=item.encryption_aad,
        )
        canonical = json.loads(
            self._payload_provider.decrypt(
                canonical_payload,
                associated_data=item.encryption_aad,
            ).decode()
        )
        display = self._payload_provider.decrypt(
            display_payload,
            associated_data=item.encryption_aad,
        ).decode()
        return canonical, display

    def _build_context(
        self,
        command: MemoryCommand,
        state: PlannerState,
        *,
        operation_id: str,
        now: datetime,
    ) -> PlannerContext:
        proposal = _candidate_for(command)
        normalized_candidate: NormalizedCandidate | None = None
        predecessor = None
        if proposal is not None:
            predecessor = self._normalization_predecessor(command, state)
            normalized_candidate = normalize_candidate(
                proposal,
                owner_id=self.owner_id,
                keyed_provider=self._fingerprint_provider,
                predecessor=predecessor.identity if predecessor is not None else None,
            )

        normalized_update = self._normalized_update(command, state)
        source = normalize_source(command.source)
        blocking_id = None
        reconfirm_ids: list[str] = []
        if normalized_candidate is not None:
            blocked = resurrection_blocked(
                state.tombstones,
                normalized_candidate.canonical_fingerprint,
                owner_id=self.owner_id,
                now=now,
                explicit_reconfirmation=normalized_candidate.explicit_user_request,
                provider=self._tombstone_provider,
            )
            blocking_id = blocked.id if blocked is not None else None
            if normalized_candidate.explicit_user_request:
                reconfirm_ids = [
                    item.id
                    for item in state.tombstones
                    if tombstone_matches(
                        item,
                        normalized_candidate.canonical_fingerprint,
                        owner_id=self.owner_id,
                        now=now,
                        provider=self._tombstone_provider,
                    )
                ]

        forget_fingerprints: list[tuple[str, Any]] = []
        if isinstance(command, ForgetMemoryCommand):
            record = state.record(str(command.target.memory_id))
            if record is not None:
                forget_fingerprints.append(
                    (
                        record.id,
                        tombstone_digest(
                            record.canonical_fingerprint,
                            owner_id=self.owner_id,
                            provider=self._tombstone_provider,
                        ),
                    )
                )
        return PlannerContext(
            owner_id=self.owner_id,
            operation_id=operation_id,
            now=now,
            source=source,
            normalized_candidate=normalized_candidate,
            normalized_update=normalized_update,
            blocking_tombstone_id=blocking_id,
            reconfirm_tombstone_ids=tuple(reconfirm_ids),
            forget_fingerprints=tuple(forget_fingerprints),
        )

    @staticmethod
    def _normalization_predecessor(
        command: MemoryCommand,
        state: PlannerState,
    ) -> RecordSnapshot | None:
        if isinstance(command, ReplaceMemoryCommand) and command.targets:
            return state.record(str(command.targets[0].memory_id))
        if isinstance(command, RestoreMemoryCommand):
            return state.record(str(command.target.memory_id))
        return None

    def _normalized_update(
        self,
        command: MemoryCommand,
        state: PlannerState,
    ) -> NormalizedRecordValue | None:
        if not isinstance(command, UpdateMemoryCommand):
            return None
        record = state.record(str(command.target.memory_id))
        if record is None:
            return None
        changes_value = bool(
            {"canonical_value", "display_text", "sensitivity"} & command.patch.model_fields_set
        )
        if not changes_value:
            return None
        canonical = (
            command.patch.canonical_value
            if "canonical_value" in command.patch.model_fields_set
            else record.canonical_value
        )
        display = (
            command.patch.display_text
            if "display_text" in command.patch.model_fields_set
            else record.display_text
        )
        sensitivity = command.patch.sensitivity or Sensitivity(record.sensitivity)
        if canonical is None or display is None:
            raise MemoryNormalizationError("update_value_material_unavailable")
        return normalize_record_value(
            owner_id=self.owner_id,
            subject_key=record.subject_key,
            memory_type=record.memory_type,
            scope_type=record.scope_type,
            scope_project_id=record.scope_project_id,
            domain_key=record.domain_key,
            slot_key=record.slot_key,
            canonical_value=canonical,
            display_text=display,
            sensitivity=sensitivity,
            value_schema_version=record.value_schema_version,
            keyed_provider=self._fingerprint_provider,
        )

    def _encrypt_command(
        self,
        command: MemoryCommand,
        context: PlannerContext,
    ) -> EncryptedValue:
        candidate = context.normalized_candidate
        memory_type = candidate.memory_type.value if candidate else "knowledge"
        domain = candidate.domain_key if candidate else "global"
        slot = candidate.slot_key if candidate else f"knowledge:global:item:{context.operation_id}"
        key_version = self._key_versions.current_encryption_key_version()
        aad = build_associated_data(
            owner_id=self.owner_id,
            memory_type=memory_type,
            domain_key=domain,
            slot_key=slot,
            record_id=context.operation_id,
            schema_version=1,
            key_version=key_version,
            purpose="operation_command",
        )
        payload = self._payload_provider.encrypt(
            json.dumps(
                command.model_dump(mode="json", exclude_none=False),
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            associated_data=aad,
        )
        if payload.key_version != key_version or payload.associated_data != aad:
            raise MemoryMutationError("encryption_provider_metadata_mismatch")
        return payload

    def _precompute_plan_crypto(
        self,
        command: MemoryCommand,
        state: PlannerState,
        context: PlannerContext,
        plan: MutationPlan,
    ) -> dict[str, dict[str, Any]]:
        material: dict[str, dict[str, Any]] = {}
        for spec in plan.records_to_create:
            if spec.candidate.sensitivity is Sensitivity.SENSITIVE:
                material[f"payload:{spec.id}"] = self._encrypted_candidate_payload(
                    spec.id, spec.candidate
                )
        for decision in plan.candidate_decisions:
            candidate_id = _record_id_for_candidate(plan, decision.candidate)
            if (
                decision.candidate.sensitivity is Sensitivity.SENSITIVE
                and f"payload:{candidate_id}" not in material
            ):
                material[f"payload:{candidate_id}"] = self._encrypted_candidate_payload(
                    candidate_id,
                    decision.candidate,
                )
        if isinstance(command, ForgetMemoryCommand):
            record = state.record(str(command.target.memory_id))
            if record is not None and record.sensitivity == Sensitivity.NORMAL.value:
                material[f"forget:{record.id}"] = self._encrypted_record_value(
                    record.id,
                    record.memory_type,
                    record.domain_key,
                    record.slot_key,
                    canonical_json_bytes(record.canonical_value),
                    (record.display_text or "").encode(),
                    purpose="forgotten_payload",
                )
        if (
            context.normalized_update
            and context.normalized_update.sensitivity is Sensitivity.SENSITIVE
        ):
            target = (
                state.record(str(command.target.memory_id))
                if isinstance(command, UpdateMemoryCommand)
                else None
            )
            if target:
                material[f"update:{target.id}"] = self._encrypted_record_value(
                    target.id,
                    target.memory_type,
                    target.domain_key,
                    target.slot_key,
                    context.normalized_update.canonical_json,
                    context.normalized_update.display_text.encode(),
                    purpose="canonical_record",
                )
        for spec in plan.sources_to_create:
            evidence = self._source_evidence(spec, context)
            excerpt = evidence.text if evidence is not None else None
            sensitivity = self._memory_sensitivity(spec.memory_id, state, plan)
            if excerpt and sensitivity is Sensitivity.SENSITIVE:
                key_version = self._key_versions.current_encryption_key_version()
                identity = self._memory_identity(spec.memory_id, state, plan)
                aad = build_associated_data(
                    owner_id=self.owner_id,
                    memory_type=identity[0],
                    domain_key=identity[1],
                    slot_key=identity[2],
                    record_id=spec.memory_id,
                    schema_version=1,
                    key_version=key_version,
                    purpose="source_excerpt",
                )
                encrypted = self._payload_provider.encrypt(excerpt.encode(), associated_data=aad)
                fingerprint_material = json.dumps(
                    {
                        "conversation_id": spec.source.conversation_id,
                        "evidence": excerpt,
                        "memory_id": spec.memory_id,
                        "message_id": spec.source.message_id,
                        "role": spec.assertion_role,
                        "session_id": spec.source.session_id,
                        "source_id": spec.source.source_id,
                        "structured": dict(spec.structured_evidence),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                digest = self._fingerprint_provider.fingerprint(
                    fingerprint_material, owner_id=self.owner_id
                )
                material[f"source:{spec.id}"] = {
                    "encrypted_excerpt": encrypted.ciphertext,
                    "excerpt_encryption_algorithm": encrypted.algorithm,
                    "excerpt_key_version": encrypted.key_version,
                    "excerpt_nonce": encrypted.nonce,
                    "excerpt_aad": aad,
                    "source_content_hash": f"keyed:{digest.key_version}:{digest.digest}",
                }
        return material

    def _commit_prepared(
        self,
        command: MemoryCommand,
        prepared: _PreparedMutation,
    ) -> MemoryCommandResult:
        session = self._sessions()
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            existing = repository.get_operation_by_idempotency_key(command.idempotency_key)
            if existing is not None:
                session.rollback()
                if existing.request_hash != prepared.request_hash:
                    return _fixed_failed_result(
                        command,
                        existing.id,
                        MemoryErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency_key_reused_with_different_request",
                    )
                return self._result_from_operation(session, existing)

            write_state = self._load_state(session, decrypt_sensitive=False)
            if self._state_guard(write_state) != self._state_guard(prepared.state):
                session.rollback()
                raise _PlanChanged("planner_state_changed_before_write")

            operation = self._operation_row(
                command,
                operation_id=prepared.context.operation_id,
                request_hash=prepared.request_hash,
                sensitivity=prepared.effective_sensitivity,
                normalized_json=prepared.normalized_command_json,
                encrypted=prepared.encrypted_command,
                now=prepared.context.now,
            )
            repository.add_operation(operation)
            self._inject("operation_start")
            self._apply_plan(session, repository, command, write_state, prepared)
            operation.status = prepared.plan.operation_status
            operation.outcome = prepared.plan.outcome.value
            operation.rejection_code = (
                prepared.plan.rejection_code.value if prepared.plan.rejection_code else None
            )
            operation.error_code = (
                prepared.plan.error_code.value if prepared.plan.error_code else None
            )
            operation.result_record_ids = list(prepared.plan.affected_memory_ids)
            committed_result = self._result_from_plan(command, prepared.plan).model_copy(
                update={"derived_state": self._derived_state_for_outcome(prepared.plan.outcome)}
            )
            operation.error_detail = _encode_replay_envelope(committed_result)
            operation.committed_at = prepared.context.now
            session.flush()
            self._inject("operation_completion")
            session.commit()
            return committed_result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _operation_row(
        self,
        command: MemoryCommandBase,
        *,
        operation_id: str,
        request_hash: str,
        sensitivity: Sensitivity,
        normalized_json: dict[str, Any] | None,
        encrypted: EncryptedValue | None,
        now: datetime,
    ) -> MemoryOperation:
        return MemoryOperation(
            id=operation_id,
            owner_id=self.owner_id,
            idempotency_key=command.idempotency_key,
            operation_kind=command.operation.value,
            actor_kind=command.actor.kind.value,
            actor_id=command.actor.actor_id,
            source_kind=command.source.kind.value,
            sensitivity=(
                Sensitivity.SENSITIVE.value
                if sensitivity is Sensitivity.SENSITIVE
                else Sensitivity.NORMAL.value
            ),
            normalized_command_json=normalized_json,
            encrypted_command_payload=encrypted.ciphertext if encrypted else None,
            encryption_algorithm=encrypted.algorithm if encrypted else None,
            encryption_key_version=encrypted.key_version if encrypted else None,
            encryption_nonce=encrypted.nonce if encrypted else None,
            encryption_aad=encrypted.associated_data if encrypted else None,
            request_hash=request_hash,
            status="started",
            outcome=None,
            rejection_code=None,
            error_code=None,
            result_record_ids=[],
            error_detail=None,
            contract_version=CONTRACT_VERSION,
            policy_version=POLICY_VERSION,
            taxonomy_version=TAXONOMY_VERSION,
            schema_version=1,
            created_at=now,
        )

    def _apply_plan(
        self,
        session: Session,
        repository: MemoryRepository,
        command: MemoryCommand,
        state: PlannerState,
        prepared: _PreparedMutation,
    ) -> None:
        plan = prepared.plan
        self._apply_candidate_decisions(repository, prepared)
        for index, spec in enumerate(plan.records_to_update):
            values = self._materialize_update_values(
                command, state, prepared, spec.id, spec.as_dict()
            )
            repository.update_record_fields(
                spec.id,
                expected_revision=spec.expected_revision,
                values=values,
            )
            if index == 0:
                self._inject("first_predecessor_transition")

        for spec in plan.records_to_create:
            repository.add_record(
                self._record_row(
                    spec.id,
                    spec.candidate,
                    plan.operation_id,
                    prepared.context.now,
                    prepared.crypto_material,
                )
            )
        if plan.records_to_create:
            self._inject("replacement_record_creation")

        for spec in plan.relations_to_create:
            repository.add_relation(
                MemoryRelation(
                    id=spec.id,
                    owner_id=self.owner_id,
                    from_memory_id=spec.from_memory_id,
                    relation_type=spec.relation_type,
                    to_memory_id=spec.to_memory_id,
                    operation_id=plan.operation_id,
                    schema_version=1,
                )
            )
        if plan.relations_to_create:
            self._inject("relation_creation")

        for spec in plan.sources_to_create:
            repository.add_source(self._source_row(spec, state, prepared))
        for spec in plan.sources_to_detach:
            session.execute(
                update(MemorySource)
                .where(
                    MemorySource.owner_id == self.owner_id,
                    MemorySource.memory_id == spec.memory_id,
                )
                .values(is_active=False, detachment_reason=spec.reason)
            )
        if plan.sources_to_create or plan.sources_to_detach:
            session.flush()
            self._inject("provenance_creation")

        for spec in plan.tombstones_to_create:
            repository.add_tombstone(
                MemoryTombstone(
                    id=spec.id,
                    owner_id=self.owner_id,
                    fingerprint_digest=spec.fingerprint.digest,
                    fingerprint_key_version=spec.fingerprint.key_version,
                    memory_type=spec.memory_type.value,
                    domain_key=spec.domain_key,
                    slot_key=spec.slot_key,
                    originating_operation_id=plan.operation_id,
                    created_at=spec.created_at,
                    expires_at=spec.expires_at,
                    explicitly_reconfirmed=False,
                    schema_version=1,
                )
            )
        for spec in plan.tombstones_to_reconfirm:
            result = session.execute(
                update(MemoryTombstone)
                .where(
                    MemoryTombstone.owner_id == self.owner_id,
                    MemoryTombstone.id == spec.id,
                )
                .values(explicitly_reconfirmed=True, reconfirmed_at=spec.reconfirmed_at)
            )
            if result.rowcount != 1:
                raise _PlanChanged("tombstone_reconfirmation_precondition_changed")
        for tombstone_id in plan.tombstones_to_delete:
            repository.delete_tombstone(tombstone_id)
        if plan.tombstones_to_create or plan.tombstones_to_reconfirm or plan.tombstones_to_delete:
            session.flush()
            self._inject("tombstone_creation")

        for spec in plan.records_to_delete:
            self._erase_record(session, spec.id, spec.expected_revision)

        for spec in plan.outbox_events:
            repository.add_outbox_event(
                MemoryOutbox(
                    id=spec.id,
                    owner_id=self.owner_id,
                    event_kind=spec.event_kind,
                    memory_id=spec.memory_id,
                    canonical_revision=spec.canonical_revision,
                    content_hash=spec.content_hash,
                    event_payload_json=dict(spec.event_payload),
                    state="pending",
                    attempts=0,
                    event_idempotency_key=spec.idempotency_key,
                    schema_version=1,
                )
            )
        if plan.outbox_events:
            self._inject("outbox_creation")

    def _apply_candidate_decisions(
        self,
        repository: MemoryRepository,
        prepared: _PreparedMutation,
    ) -> None:
        for decision in prepared.plan.candidate_decisions:
            candidate = decision.candidate
            existing = repository.get_candidate(candidate.candidate_id)
            if existing is not None:
                repository.update_candidate_decision(
                    candidate.candidate_id,
                    expected_revision=existing.revision,
                    values={
                        "state": decision.state.value,
                        "decision_outcome": decision.outcome.value,
                        "decision_rejection_code": (
                            decision.rejection_code.value if decision.rejection_code else None
                        ),
                        "decision_error_code": None,
                        "decision_reason": decision.reason,
                        "applied_operation_id": prepared.plan.operation_id,
                        "decided_at": prepared.context.now,
                    },
                )
                continue
            payload = self._candidate_payload_fields(
                _record_id_for_candidate(prepared.plan, candidate),
                candidate,
                prepared.crypto_material,
            )
            repository.add_candidate(
                MemoryCandidate(
                    id=candidate.candidate_id,
                    owner_id=self.owner_id,
                    subject_key=candidate.subject_key,
                    memory_type=candidate.memory_type.value,
                    domain_key=candidate.domain_key,
                    slot_key=candidate.slot_key,
                    cardinality=candidate.cardinality.value,
                    sensitivity=candidate.sensitivity.value,
                    **payload,
                    intent=candidate.intent.value,
                    target_hints_json=candidate.target_hints,
                    trusted_target_ids=list(candidate.target_ids),
                    predecessor_evidence_json={},
                    source_spans_json=[
                        {
                            "role": item.role.value,
                            "start": item.start,
                            "end": item.end,
                        }
                        for item in candidate.evidence
                    ],
                    grounding_evidence_json={},
                    confidence=candidate.confidence,
                    importance=candidate.importance,
                    explicit_user_request=candidate.explicit_user_request,
                    extractor_name="structured-command",
                    extractor_version="memory-mutation-v1",
                    raw_output_hash=None,
                    state=decision.state.value,
                    decision_outcome=decision.outcome.value,
                    decision_rejection_code=(
                        decision.rejection_code.value if decision.rejection_code else None
                    ),
                    decision_error_code=None,
                    decision_reason=decision.reason,
                    applied_operation_id=prepared.plan.operation_id,
                    decided_at=prepared.context.now,
                    revision=1,
                    contract_version=CONTRACT_VERSION,
                    policy_version=POLICY_VERSION,
                    taxonomy_version=TAXONOMY_VERSION,
                    value_schema_version=candidate.value_schema_version,
                    candidate_schema_version=1,
                )
            )

    def _candidate_payload_fields(
        self,
        record_id: str,
        candidate: NormalizedCandidate,
        crypto_material: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if candidate.sensitivity is Sensitivity.NORMAL:
            return {
                "canonical_payload": candidate.canonical_value,
                "display_text": candidate.display_text,
                "encrypted_canonical_payload": None,
                "encrypted_display_payload": None,
                "encryption_algorithm": None,
                "encryption_key_version": None,
                "canonical_nonce": None,
                "display_nonce": None,
                "encryption_aad": None,
            }
        try:
            return crypto_material[f"payload:{record_id}"]
        except KeyError as exc:
            raise MemoryMutationError("prepared_sensitive_payload_missing") from exc

    def _encrypted_candidate_payload(
        self,
        record_id: str,
        candidate: NormalizedCandidate,
    ) -> dict[str, Any]:
        encrypted = self._encrypted_record_value(
            record_id,
            candidate.memory_type,
            candidate.domain_key,
            candidate.slot_key,
            candidate.canonical_json,
            candidate.display_text.encode(),
            purpose="candidate_payload",
        )
        return {
            "canonical_payload": None,
            "display_text": None,
            **encrypted,
        }

    def _record_row(
        self,
        record_id: str,
        candidate: NormalizedCandidate,
        operation_id: str,
        now: datetime,
        crypto_material: dict[str, dict[str, Any]],
    ) -> MemoryRecord:
        payload = self._candidate_payload_fields(record_id, candidate, crypto_material)
        return MemoryRecord(
            id=record_id,
            owner_id=self.owner_id,
            subject_key=candidate.subject_key,
            memory_type=candidate.memory_type.value,
            scope_type=candidate.scope_type,
            scope_project_id=candidate.scope_project_id,
            domain_key=candidate.domain_key,
            slot_key=candidate.slot_key,
            cardinality=candidate.cardinality.value,
            sensitivity=candidate.sensitivity.value,
            **payload,
            canonical_fingerprint=candidate.canonical_fingerprint,
            confidence=candidate.confidence,
            importance=candidate.importance,
            status=MemoryLifecycleState.ACTIVE.value,
            last_confirmed_at=now,
            usage_count=0,
            pinned=False,
            created_by_operation_id=operation_id,
            revision=1,
            metadata_json={},
            contract_version=CONTRACT_VERSION,
            taxonomy_version=TAXONOMY_VERSION,
            policy_version=POLICY_VERSION,
            value_schema_version=candidate.value_schema_version,
            record_schema_version=1,
        )

    def _encrypted_record_value(
        self,
        record_id: str,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
        canonical: bytes,
        display: bytes,
        *,
        purpose: str,
    ) -> dict[str, Any]:
        key_version = self._key_versions.current_encryption_key_version()
        aad = build_associated_data(
            owner_id=self.owner_id,
            memory_type=memory_type.value,
            domain_key=domain_key,
            slot_key=slot_key,
            record_id=record_id,
            schema_version=1,
            key_version=key_version,
            purpose=purpose,
        )
        encrypted_canonical = self._payload_provider.encrypt(canonical, associated_data=aad)
        encrypted_display = self._payload_provider.encrypt(display, associated_data=aad)
        if any(
            item.key_version != key_version or item.associated_data != aad
            for item in (encrypted_canonical, encrypted_display)
        ):
            raise MemoryMutationError("encryption_provider_metadata_mismatch")
        if encrypted_canonical.algorithm != encrypted_display.algorithm:
            raise MemoryMutationError("encryption_provider_algorithm_mismatch")
        return {
            "encrypted_canonical_payload": encrypted_canonical.ciphertext,
            "encrypted_display_payload": encrypted_display.ciphertext,
            "encryption_algorithm": encrypted_canonical.algorithm,
            "encryption_key_version": key_version,
            "canonical_nonce": encrypted_canonical.nonce,
            "display_nonce": encrypted_display.nonce,
            "encryption_aad": aad,
        }

    def _materialize_update_values(
        self,
        command: MemoryCommand,
        state: PlannerState,
        prepared: _PreparedMutation,
        record_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        record = state.record(record_id)
        if record is None:
            raise _PlanChanged("record_disappeared_before_update")
        normalized = prepared.context.normalized_update
        if normalized is not None and record_id == str(command.target.memory_id):
            if normalized.sensitivity is Sensitivity.SENSITIVE:
                try:
                    encrypted = prepared.crypto_material[f"update:{record.id}"]
                except KeyError as exc:
                    raise MemoryMutationError("prepared_update_payload_missing") from exc
                values.update(
                    canonical_payload=None,
                    display_text=None,
                    **encrypted,
                )
            else:
                values.update(
                    canonical_payload=normalized.canonical_value,
                    display_text=normalized.display_text,
                    encrypted_canonical_payload=None,
                    encrypted_display_payload=None,
                    encryption_algorithm=None,
                    encryption_key_version=None,
                    canonical_nonce=None,
                    display_nonce=None,
                    encryption_aad=None,
                )
        if isinstance(command, ForgetMemoryCommand) and record_id == str(command.target.memory_id):
            if record.sensitivity == Sensitivity.NORMAL.value:
                try:
                    encrypted = prepared.crypto_material[f"forget:{record.id}"]
                except KeyError as exc:
                    raise MemoryMutationError("prepared_forget_payload_missing") from exc
                values.update(
                    canonical_payload=None,
                    display_text=None,
                    sensitivity=Sensitivity.SENSITIVE.value,
                    **encrypted,
                )
        return values

    def _source_row(
        self,
        spec: SourceCreateSpec,
        state: PlannerState,
        prepared: _PreparedMutation,
    ) -> MemorySource:
        item = self._source_evidence(spec, prepared.context)
        excerpt = item.text if item else None
        memory_sensitivity = self._memory_sensitivity(spec.memory_id, state, prepared.plan)
        excerpt_fields: dict[str, Any] = {
            "redacted_excerpt": None,
            "encrypted_excerpt": None,
            "excerpt_encryption_algorithm": None,
            "excerpt_key_version": None,
            "excerpt_nonce": None,
            "excerpt_aad": None,
        }
        if excerpt and memory_sensitivity is Sensitivity.SENSITIVE:
            try:
                prepared_source = prepared.crypto_material[f"source:{spec.id}"]
            except KeyError as exc:
                raise MemoryMutationError("prepared_source_payload_missing") from exc
            source_hash = str(prepared_source["source_content_hash"])
            excerpt_fields.update(
                {
                    key: value
                    for key, value in prepared_source.items()
                    if key != "source_content_hash"
                }
            )
        else:
            excerpt_fields["redacted_excerpt"] = excerpt
            material = json.dumps(
                {
                    "conversation_id": spec.source.conversation_id,
                    "evidence": excerpt,
                    "memory_id": spec.memory_id,
                    "message_id": spec.source.message_id,
                    "role": spec.assertion_role,
                    "session_id": spec.source.session_id,
                    "source_id": spec.source.source_id,
                    "structured": dict(spec.structured_evidence),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            source_hash = hashlib.sha256(material).hexdigest()
        return MemorySource(
            id=spec.id,
            owner_id=self.owner_id,
            memory_id=spec.memory_id,
            source_kind=spec.source.kind,
            source_id=spec.source.source_id,
            conversation_id=spec.source.conversation_id,
            session_id=spec.source.session_id,
            message_id=spec.source.message_id,
            source_span_json={
                "role": item.role.value if item else None,
                "start": item.start if item else None,
                "end": item.end if item else None,
                **dict(spec.structured_evidence),
            },
            **excerpt_fields,
            source_content_hash=source_hash,
            source_timestamp=None,
            observed_at=spec.source.observed_at or prepared.context.now,
            extractor_version="memory-structured-v1",
            assertion_role=spec.assertion_role,
            is_active=True,
            detachment_reason=None,
            operation_id=prepared.plan.operation_id,
            schema_version=1,
        )

    @staticmethod
    def _source_evidence(spec: SourceCreateSpec, context: PlannerContext):
        evidence = tuple(
            item
            for item in (
                *spec.source.evidence,
                *(context.normalized_candidate.evidence if context.normalized_candidate else ()),
            )
            if (
                item.role is EvidenceRole.ASSERTION
                if spec.assertion_role == "supports"
                else item.role is EvidenceRole.RETRACTION
            )
        )
        return evidence[spec.evidence_index] if spec.evidence_index is not None else None

    @staticmethod
    def _memory_sensitivity(
        memory_id: str,
        state: PlannerState,
        plan: MutationPlan,
    ) -> Sensitivity:
        created = next(
            (item.candidate for item in plan.records_to_create if item.id == memory_id),
            None,
        )
        if created:
            return created.sensitivity
        existing = state.record(memory_id)
        return Sensitivity(existing.sensitivity) if existing else Sensitivity.NORMAL

    @staticmethod
    def _memory_identity(
        memory_id: str,
        state: PlannerState,
        plan: MutationPlan,
    ) -> tuple[str, str, str]:
        created = next(
            (item.candidate for item in plan.records_to_create if item.id == memory_id),
            None,
        )
        if created:
            return created.memory_type.value, created.domain_key, created.slot_key
        existing = state.record(memory_id)
        if existing is None:
            raise _PlanChanged("source_memory_identity_unavailable")
        return existing.memory_type.value, existing.domain_key, existing.slot_key

    @staticmethod
    def _state_guard(state: PlannerState) -> tuple[Any, ...]:
        records = tuple(
            sorted(
                (
                    item.id,
                    item.owner_id,
                    item.subject_key,
                    item.memory_type.value,
                    item.domain_key,
                    item.slot_key,
                    item.cardinality.value,
                    item.sensitivity,
                    item.canonical_fingerprint,
                    item.status.value,
                    item.revision,
                    item.value_schema_version,
                )
                for item in state.records
            )
        )
        candidates = tuple(sorted((item.id, item.state.value) for item in state.candidates))
        relations = tuple(
            sorted(
                (item.from_memory_id, item.relation_type, item.to_memory_id)
                for item in state.relations
            )
        )
        tombstones = tuple(
            sorted(
                (
                    item.id,
                    item.owner_id,
                    item.fingerprint_digest,
                    item.fingerprint_key_version,
                    item.memory_type,
                    item.domain_key,
                    item.slot_key,
                    item.expires_at.isoformat(),
                    item.explicitly_reconfirmed,
                )
                for item in state.tombstones
            )
        )
        return records, candidates, relations, tombstones

    def _erase_record(self, session: Session, memory_id: str, expected_revision: int) -> None:
        record = session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.id == memory_id,
                MemoryRecord.revision == expected_revision,
            )
        )
        if record is None:
            raise MemoryRevisionConflict("erase_revision_conflict_or_not_found")
        session.execute(
            delete(MemoryOutbox).where(
                MemoryOutbox.owner_id == self.owner_id,
                MemoryOutbox.memory_id == memory_id,
            )
        )
        operations = session.scalars(
            select(MemoryOperation).where(MemoryOperation.owner_id == self.owner_id)
        ).all()
        applied_candidate_operation_ids = [
            operation.id
            for operation in operations
            if operation.result_record_ids and operation.result_record_ids[0] == memory_id
        ]
        session.execute(
            delete(MemoryCandidate).where(
                MemoryCandidate.owner_id == self.owner_id,
                MemoryCandidate.applied_operation_id.in_(applied_candidate_operation_ids),
            )
        )
        session.delete(record)
        for operation in operations:
            if memory_id not in (operation.result_record_ids or []):
                continue
            operation.sensitivity = Sensitivity.NORMAL.value
            operation.normalized_command_json = {
                "operation": operation.operation_kind,
                "redacted_by_permanent_erasure": True,
            }
            operation.encrypted_command_payload = None
            operation.encryption_algorithm = None
            operation.encryption_key_version = None
            operation.encryption_nonce = None
            operation.encryption_aad = None
            operation.request_hash = f"erased:{operation.id}"
            operation.result_record_ids = [
                item for item in operation.result_record_ids if item != memory_id
            ]
            operation.error_detail = None
        session.flush()

    def _result_from_operation(
        self,
        session: Session,
        operation: MemoryOperation,
    ) -> MemoryCommandResult:
        outcome = MemoryOutcome(operation.outcome or MemoryOutcome.FAILED.value)
        ids = tuple(UUID(item) for item in (operation.result_record_ids or []))
        replay = _decode_replay_envelope(operation.error_detail)
        active_ids: tuple[UUID, ...] = ()
        active_outcomes = {
            MemoryOutcome.CREATED,
            MemoryOutcome.RECONFIRMED,
            MemoryOutcome.REFINED,
            MemoryOutcome.REPLACED,
            MemoryOutcome.SUPERSEDED,
            MemoryOutcome.MERGED,
            MemoryOutcome.RESTORED,
        }
        if replay is not None:
            try:
                active_ids = tuple(UUID(item) for item in replay.get("a", ()))
            except (TypeError, ValueError):
                replay = None
                active_ids = ()
        if replay is None and outcome in active_outcomes and ids:
            active_ids = (ids[0],)
        current_revision = replay.get("r") if replay is not None else None
        if replay is None and ids:
            current_revision = session.scalar(
                select(MemoryRecord.revision).where(
                    MemoryRecord.owner_id == self.owner_id,
                    MemoryRecord.id == str(ids[0]),
                )
            )
        candidate_id = replay.get("c") if replay is not None else None
        if replay is None:
            candidate_id = session.scalar(
                select(MemoryCandidate.id).where(
                    MemoryCandidate.owner_id == self.owner_id,
                    MemoryCandidate.applied_operation_id == operation.id,
                )
            )
        derived = (
            DerivedState(replay["d"])
            if replay is not None
            else self._derived_state_for_outcome(outcome)
        )
        return MemoryCommandResult(
            operation_id=UUID(operation.id),
            owner_id=operation.owner_id,
            operation=MemoryOperationKind(operation.operation_kind),
            outcome=outcome,
            affected_memory_ids=ids,
            active_memory_ids=active_ids,
            candidate_id=UUID(candidate_id) if candidate_id else None,
            current_revision=current_revision,
            rejection_code=(
                MemoryRejectionCode(operation.rejection_code) if operation.rejection_code else None
            ),
            error_code=(MemoryErrorCode(operation.error_code) if operation.error_code else None),
            derived_state=derived,
            message=replay.get("m") if replay is not None else operation.error_detail,
        )

    @staticmethod
    def _derived_state_for_outcome(outcome: MemoryOutcome) -> DerivedState:
        if outcome in {
            MemoryOutcome.CREATED,
            MemoryOutcome.RECONFIRMED,
            MemoryOutcome.REFINED,
            MemoryOutcome.REPLACED,
            MemoryOutcome.SUPERSEDED,
            MemoryOutcome.MERGED,
            MemoryOutcome.ARCHIVED,
            MemoryOutcome.FORGOTTEN,
            MemoryOutcome.ERASED_PERMANENTLY,
            MemoryOutcome.RESTORED,
        }:
            return DerivedState.PENDING
        return DerivedState.NOT_APPLICABLE

    @staticmethod
    def _result_from_plan(
        command: MemoryCommandBase,
        plan: MutationPlan,
    ) -> MemoryCommandResult:
        return MemoryCommandResult(
            operation_id=UUID(plan.operation_id),
            owner_id=command.owner_id,
            operation=command.operation,
            outcome=plan.outcome,
            affected_memory_ids=tuple(UUID(item) for item in plan.affected_memory_ids),
            active_memory_ids=tuple(UUID(item) for item in plan.active_memory_ids),
            candidate_id=(
                UUID(plan.candidate_decisions[0].candidate.candidate_id)
                if plan.candidate_decisions
                else None
            ),
            current_revision=plan.current_revision,
            rejection_code=plan.rejection_code,
            error_code=plan.error_code,
            derived_state=DerivedState.NOT_APPLICABLE,
            message=plan.message,
        )

    def _inject(self, stage: str) -> None:
        if self._failure_injector is None:
            return
        try:
            self._failure_injector(stage)
        except Exception as exc:
            raise InjectedMutationFailure("injected_mutation_failure") from exc


def _record_id_for_candidate(plan: MutationPlan, candidate: NormalizedCandidate) -> str:
    created = next(
        (item.id for item in plan.records_to_create if item.candidate == candidate),
        None,
    )
    return created or plan.operation_id
