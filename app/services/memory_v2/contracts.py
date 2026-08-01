"""SQLAlchemy-free command and result contracts for Neo personal memory v2."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.core.identifiers import canonical_uuid
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from app.services.memory_v2.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION

NonEmptyText = Annotated[str, Field(min_length=1)]
OwnerId = Annotated[str, BeforeValidator(canonical_uuid), Field(min_length=36, max_length=36)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200)]
Revision = Annotated[int, Field(ge=1)]
Confidence = Annotated[float, Field(ge=0, le=1)]
Importance = Annotated[int, Field(ge=1, le=10)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class MemoryOperationKind(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    SUPERSEDE = "supersede"
    MERGE = "merge"
    ARCHIVE = "archive"
    FORGET = "forget"
    ERASE_PERMANENTLY = "erase_permanently"
    RESTORE = "restore"


class MemoryOutcome(StrEnum):
    CREATED = "created"
    RECONFIRMED = "reconfirmed"
    REFINED = "refined"
    REPLACED = "replaced"
    SUPERSEDED = "superseded"
    MERGED = "merged"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"
    ERASED_PERMANENTLY = "erased_permanently"
    RESTORED = "restored"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    DISABLED = "disabled"
    FAILED = "failed"


class SourceChangeAction(StrEnum):
    DETACH_SOURCE = "detach_source"


class SourceChangeOutcome(StrEnum):
    PRESERVED = "preserved"
    NEEDS_REVIEW = "needs_review"
    SOURCE_NOT_FOUND = "source_not_found"
    ALREADY_DETACHED = "already_detached"
    OWNER_MISMATCH = "owner_mismatch"
    REVISION_CONFLICT = "revision_conflict"


class CandidatePersistenceOutcome(StrEnum):
    PERSISTED = "persisted"
    ALREADY_EXISTS = "already_exists"
    PROHIBITED = "prohibited"
    REJECTED = "rejected"


class MemoryRejectionCode(StrEnum):
    AMBIGUOUS_CONFLICT = "ambiguous_conflict"
    CONFLICT_REQUIRES_REPLACE = "conflict_requires_replace"
    INCOGNITO_DISABLED = "incognito_disabled"
    MEMORY_DISABLED = "memory_disabled"
    PROHIBITED_SENSITIVE_CONTENT = "prohibited_sensitive_content"
    SENSITIVE_REQUIRES_EXPLICIT_REQUEST = "sensitive_requires_explicit_request"
    RESURRECTION_BLOCKED = "resurrection_blocked"
    REPLACEMENT_TARGET_NOT_FOUND = "replacement_target_not_found"
    POSITIVE_VALUE_REQUIRED = "positive_value_required"
    UNGROUNDED_CANDIDATE = "ungrounded_candidate"
    TOO_MANY_CANDIDATES = "too_many_candidates"
    INVALID_RESTORE = "invalid_restore"


class MemoryErrorCode(StrEnum):
    INVALID_COMMAND = "invalid_command"
    OWNER_REQUIRED = "owner_required"
    OWNER_MISMATCH = "owner_mismatch"
    CROSS_OWNER_REFERENCE = "cross_owner_reference"
    NOT_FOUND = "not_found"
    EXPECTED_REVISION_REQUIRED = "expected_revision_required"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    UNSUPPORTED_CONTRACT_VERSION = "unsupported_contract_version"
    UNSUPPORTED_POLICY_VERSION = "unsupported_policy_version"
    UNSUPPORTED_TAXONOMY_VERSION = "unsupported_taxonomy_version"
    INTERNAL_ERROR = "internal_error"


class ActorKind(StrEnum):
    USER = "user"
    SYSTEM = "system"
    AGENT = "agent"
    MIGRATION = "migration"
    MAINTENANCE = "maintenance"


class SourceKind(StrEnum):
    CHAT_MESSAGE = "chat_message"
    DIRECT_COMMAND = "direct_command"
    MANUAL_UI = "manual_ui"
    HTTP_API = "http_api"
    AGENT_TOOL = "agent_tool"
    AUTOMATIC_EXTRACTION = "automatic_extraction"
    REVIEW = "review"
    IMPORT = "import"
    MIGRATION = "migration"
    MAINTENANCE = "maintenance"


class MemoryLifecycleState(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    FORGOTTEN = "forgotten"


class CandidateLifecycleState(StrEnum):
    PROPOSED = "proposed"
    VALIDATED = "validated"
    NEEDS_REVIEW = "needs_review"
    APPLIED = "applied"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    PROHIBITED = "prohibited"


class CandidateIntent(StrEnum):
    ASSERT = "assert"
    REPLACE = "replace"


class EvidenceRole(StrEnum):
    ASSERTION = "assertion"
    RETRACTION = "retraction"


class ReplacementAuthority(StrEnum):
    EXPLICIT_CORRECTION = "explicit_correction"
    LINKED_RETRACTION_REPLACEMENT = "linked_retraction_replacement"
    GROUNDED_SAME_SLOT_ASSERTION = "grounded_same_slot_assertion"
    REVIEWED = "reviewed"


class RestoreMode(StrEnum):
    ARCHIVED_ONLY = "archived_only"
    AS_REPLACEMENT = "as_replacement"


class DerivedState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    CURRENT = "current"
    DEGRADED = "degraded"


class MemoryActor(ContractModel):
    kind: ActorKind
    actor_id: NonEmptyText


class EvidenceSpan(ContractModel):
    role: EvidenceRole
    text: NonEmptyText
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_offsets(self) -> EvidenceSpan:
        if (self.start is None) != (self.end is None):
            raise ValueError("span_start_and_end_must_be_supplied_together")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("span_end_must_follow_start")
        return self


class MemorySource(ContractModel):
    kind: SourceKind
    source_id: str | None = Field(default=None, min_length=1, max_length=200)
    session_id: str | None = Field(default=None, min_length=1, max_length=200)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)
    message_id: str | None = Field(default=None, min_length=1, max_length=200)
    observed_at: datetime | None = None
    evidence: tuple[EvidenceSpan, ...] = ()


class CandidateTargetHints(ContractModel):
    target_memory_ids: tuple[UUID, ...] = ()
    old_value_phrases: tuple[NonEmptyText, ...] = ()
    predecessor_domain_key: str | None = Field(default=None, min_length=1)
    predecessor_slot_key: str | None = Field(default=None, min_length=1)
    explicit_domain_change: bool = False
    explicit_slot_change: bool = False

    @property
    def has_target_evidence(self) -> bool:
        return bool(
            self.target_memory_ids
            or self.old_value_phrases
            or self.predecessor_domain_key
            or self.predecessor_slot_key
        )


class CandidateProposal(ContractModel):
    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    taxonomy_version: Literal[TAXONOMY_VERSION] = TAXONOMY_VERSION
    policy_version: Literal[POLICY_VERSION] = POLICY_VERSION
    proposal_id: UUID = Field(default_factory=uuid4)
    candidate_state: CandidateLifecycleState = CandidateLifecycleState.PROPOSED
    intent: CandidateIntent = CandidateIntent.ASSERT
    subject_key: NonEmptyText = "user"
    memory_type: MemoryType
    domain_key: NonEmptyText
    slot_key: NonEmptyText
    cardinality: Cardinality
    canonical_value: JsonValue
    display_text: NonEmptyText
    sensitivity: Sensitivity
    confidence: Confidence
    importance: Importance
    value_schema_version: int = Field(default=1, ge=1)
    explicit_user_request: bool = False
    target_hints: CandidateTargetHints = Field(default_factory=CandidateTargetHints)
    evidence: tuple[EvidenceSpan, ...] = ()

    @field_validator("canonical_value")
    @classmethod
    def canonical_value_must_be_present(cls, value: JsonValue) -> JsonValue:
        if value is None:
            raise ValueError("positive_canonical_value_required")
        if isinstance(value, str) and not value.strip():
            raise ValueError("positive_canonical_value_required")
        return value


class ValidatedCandidateProposal(CandidateProposal):
    candidate_state: Literal[CandidateLifecycleState.VALIDATED] = CandidateLifecycleState.VALIDATED

    @model_validator(mode="after")
    def validate_durable_sensitivity(self) -> ValidatedCandidateProposal:
        if self.sensitivity is Sensitivity.PROHIBITED:
            raise ValueError("prohibited_candidate_cannot_be_validated")
        if self.sensitivity is Sensitivity.SENSITIVE and not self.explicit_user_request:
            raise ValueError("sensitive_candidate_requires_explicit_user_request")
        return self


class TargetRevision(ContractModel):
    memory_id: UUID
    expected_revision: Revision


class CanonicalMemorySnapshot(ContractModel):
    """Owner-bound canonical query result for deterministic planning, never model input."""

    memory_id: UUID
    owner_id: OwnerId
    subject_key: NonEmptyText
    memory_type: MemoryType
    domain_key: NonEmptyText
    slot_key: NonEmptyText
    cardinality: Cardinality
    canonical_value: JsonValue
    display_text: NonEmptyText
    sensitivity: Sensitivity
    status: MemoryLifecycleState
    revision: Revision


class CandidateGroundingSpan(ContractModel):
    message_id: NonEmptyText
    role: EvidenceRole
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_offsets(self) -> CandidateGroundingSpan:
        if self.end <= self.start:
            raise ValueError("span_end_must_follow_start")
        return self


class DetachMemorySourceCommand(ContractModel):
    """Detach one exact provenance row without changing canonical memory state."""

    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    owner_id: OwnerId
    idempotency_key: IdempotencyKey
    target: TargetRevision
    source_id: UUID
    detachment_reason: NonEmptyText = "source_deleted"


class SourceChangeResult(ContractModel):
    """Stable result for an exact persisted source detachment request."""

    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    action: Literal[SourceChangeAction.DETACH_SOURCE] = SourceChangeAction.DETACH_SOURCE
    outcome: SourceChangeOutcome
    owner_id: OwnerId
    memory_id: UUID
    requested_source_id: UUID
    detached_source_id: UUID | None = None
    remaining_active_source_count: int | None = Field(default=None, ge=0)
    memory_revision: int | None = Field(default=None, ge=1)
    canonical_mutation_performed: Literal[False] = False
    canonical_revision_changed: Literal[False] = False
    review_required: bool
    idempotency_key: IdempotencyKey
    reason: NonEmptyText

    @model_validator(mode="after")
    def validate_source_change_result(self) -> SourceChangeResult:
        detached_outcomes = {
            SourceChangeOutcome.PRESERVED,
            SourceChangeOutcome.NEEDS_REVIEW,
            SourceChangeOutcome.ALREADY_DETACHED,
        }
        if self.outcome in detached_outcomes and self.detached_source_id is None:
            raise ValueError("detached_outcome_requires_source_id")
        if self.outcome not in detached_outcomes and self.detached_source_id is not None:
            raise ValueError("non_detached_outcome_cannot_claim_detachment")
        if self.outcome is SourceChangeOutcome.PRESERVED:
            if not self.remaining_active_source_count:
                raise ValueError("preserved_source_change_requires_remaining_support")
            if self.review_required:
                raise ValueError("preserved_source_change_cannot_require_review")
        if self.outcome is SourceChangeOutcome.NEEDS_REVIEW:
            if self.remaining_active_source_count != 0 or not self.review_required:
                raise ValueError("final_source_change_requires_review")
        if (
            self.outcome
            in {
                SourceChangeOutcome.SOURCE_NOT_FOUND,
                SourceChangeOutcome.OWNER_MISMATCH,
                SourceChangeOutcome.REVISION_CONFLICT,
            }
            and self.review_required
        ):
            raise ValueError("non_applied_source_change_cannot_require_review")
        return self


class PersistExtractionCandidateCommand(ContractModel):
    """Persist a grounded non-canonical candidate through the mutation boundary."""

    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    owner_id: OwnerId
    candidate: ValidatedCandidateProposal
    state: Literal[
        CandidateLifecycleState.VALIDATED,
        CandidateLifecycleState.NEEDS_REVIEW,
    ]
    decision_outcome: MemoryOutcome | None = None
    rejection_code: MemoryRejectionCode | None = None
    decision_reason: NonEmptyText
    source_message_id: NonEmptyText
    source_spans: tuple[CandidateGroundingSpan, ...] = Field(min_length=1)
    predecessor_evidence: dict[str, JsonValue] = Field(default_factory=dict)
    grounding_evidence: dict[str, JsonValue] = Field(default_factory=dict)
    extractor_name: NonEmptyText
    extractor_version: NonEmptyText
    raw_output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_candidate_state(self) -> PersistExtractionCandidateCommand:
        if self.state is CandidateLifecycleState.NEEDS_REVIEW:
            if (
                self.decision_outcome is not MemoryOutcome.NEEDS_REVIEW
                or self.rejection_code is None
            ):
                raise ValueError("review_candidate_requires_outcome_and_code")
        elif self.decision_outcome is not None or self.rejection_code is not None:
            raise ValueError("validated_candidate_cannot_have_decision")
        return self


class CandidatePersistenceResult(ContractModel):
    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    outcome: CandidatePersistenceOutcome
    owner_id: OwnerId
    candidate_id: UUID
    state: CandidateLifecycleState | None = None
    revision: int | None = Field(default=None, ge=1)
    applied_operation_id: UUID | None = None
    reason: NonEmptyText


class CandidateStatusSnapshot(ContractModel):
    owner_id: OwnerId
    candidate_id: UUID
    state: CandidateLifecycleState
    revision: Revision
    decision_outcome: MemoryOutcome | None = None
    rejection_code: MemoryRejectionCode | None = None
    applied_operation_id: UUID | None = None


class MemoryUpdatePatch(ContractModel):
    canonical_value: JsonValue | None = None
    display_text: str | None = Field(default=None, min_length=1)
    sensitivity: Sensitivity | None = None
    confidence: Confidence | None = None
    importance: Importance | None = None
    pinned: bool | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_a_change(self) -> MemoryUpdatePatch:
        if not self.model_fields_set:
            raise ValueError("update_patch_requires_a_field")
        if "canonical_value" in self.model_fields_set and self.canonical_value is None:
            raise ValueError("canonical_value_cannot_be_null")
        return self


class MemoryCommandBase(ContractModel):
    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    policy_version: Literal[POLICY_VERSION] = POLICY_VERSION
    taxonomy_version: Literal[TAXONOMY_VERSION] = TAXONOMY_VERSION
    owner_id: OwnerId
    idempotency_key: IdempotencyKey
    actor: MemoryActor
    source: MemorySource
    dry_run: bool = False


class CreateMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.CREATE] = MemoryOperationKind.CREATE
    candidate: ValidatedCandidateProposal


class UpdateMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.UPDATE] = MemoryOperationKind.UPDATE
    target: TargetRevision
    patch: MemoryUpdatePatch


class ReplaceMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.REPLACE] = MemoryOperationKind.REPLACE
    candidate: ValidatedCandidateProposal
    authority: ReplacementAuthority
    targets: tuple[TargetRevision, ...] = ()

    @model_validator(mode="after")
    def require_replacement_evidence(self) -> ReplaceMemoryCommand:
        if self.candidate.intent is not CandidateIntent.REPLACE:
            raise ValueError("replace_command_requires_replace_candidate")
        if not self.targets and not self.candidate.target_hints.has_target_evidence:
            if self.authority is not ReplacementAuthority.GROUNDED_SAME_SLOT_ASSERTION:
                raise ValueError("replace_command_requires_target_evidence")
        return self


class SupersedeMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.SUPERSEDE] = MemoryOperationKind.SUPERSEDE
    predecessors: tuple[TargetRevision, ...] = Field(min_length=1)
    successor_memory_id: UUID


class MergeMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.MERGE] = MemoryOperationKind.MERGE
    sources: tuple[TargetRevision, ...] = Field(min_length=2)
    candidate: ValidatedCandidateProposal


class ArchiveMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.ARCHIVE] = MemoryOperationKind.ARCHIVE
    target: TargetRevision


class ForgetMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.FORGET] = MemoryOperationKind.FORGET
    target: TargetRevision


class ErasePermanentlyMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.ERASE_PERMANENTLY] = (
        MemoryOperationKind.ERASE_PERMANENTLY
    )
    target: TargetRevision


class RestoreMemoryCommand(MemoryCommandBase):
    operation: Literal[MemoryOperationKind.RESTORE] = MemoryOperationKind.RESTORE
    target: TargetRevision
    mode: RestoreMode = RestoreMode.ARCHIVED_ONLY
    replacement_candidate: ValidatedCandidateProposal | None = None

    @model_validator(mode="after")
    def validate_restore_mode(self) -> RestoreMemoryCommand:
        if self.mode is RestoreMode.AS_REPLACEMENT and self.replacement_candidate is None:
            raise ValueError("restore_as_replacement_requires_candidate")
        if self.mode is RestoreMode.ARCHIVED_ONLY and self.replacement_candidate is not None:
            raise ValueError("archived_restore_cannot_include_replacement_candidate")
        return self


MemoryCommand = Annotated[
    CreateMemoryCommand
    | UpdateMemoryCommand
    | ReplaceMemoryCommand
    | SupersedeMemoryCommand
    | MergeMemoryCommand
    | ArchiveMemoryCommand
    | ForgetMemoryCommand
    | ErasePermanentlyMemoryCommand
    | RestoreMemoryCommand,
    Field(discriminator="operation"),
]
MEMORY_COMMAND_ADAPTER = TypeAdapter(MemoryCommand)


class MemoryCommandResult(ContractModel):
    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    operation_id: UUID = Field(default_factory=uuid4)
    owner_id: OwnerId
    operation: MemoryOperationKind
    outcome: MemoryOutcome
    affected_memory_ids: tuple[UUID, ...] = ()
    active_memory_ids: tuple[UUID, ...] = ()
    candidate_id: UUID | None = None
    current_revision: int | None = Field(default=None, ge=1)
    rejection_code: MemoryRejectionCode | None = None
    error_code: MemoryErrorCode | None = None
    derived_state: DerivedState = DerivedState.NOT_APPLICABLE
    message: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_result_codes(self) -> MemoryCommandResult:
        rejection_outcomes = {
            MemoryOutcome.NEEDS_REVIEW,
            MemoryOutcome.REJECTED,
            MemoryOutcome.DISABLED,
        }
        if self.outcome in rejection_outcomes and self.rejection_code is None:
            raise ValueError("rejection_outcome_requires_rejection_code")
        if self.outcome not in rejection_outcomes and self.rejection_code is not None:
            raise ValueError("successful_or_failed_outcome_cannot_have_rejection_code")
        if self.outcome is MemoryOutcome.FAILED and self.error_code is None:
            raise ValueError("failed_outcome_requires_error_code")
        if self.outcome is not MemoryOutcome.FAILED and self.error_code is not None:
            raise ValueError("non_failed_outcome_cannot_have_error_code")
        return self

    @classmethod
    def disabled_for(
        cls,
        command: MemoryCommandBase,
        *,
        rejection_code: MemoryRejectionCode,
        message: str,
    ) -> MemoryCommandResult:
        if rejection_code not in {
            MemoryRejectionCode.INCOGNITO_DISABLED,
            MemoryRejectionCode.MEMORY_DISABLED,
        }:
            raise ValueError("disabled_result_requires_disabled_rejection_code")
        return cls(
            owner_id=command.owner_id,
            operation=command.operation,
            outcome=MemoryOutcome.DISABLED,
            rejection_code=rejection_code,
            message=message,
        )


class CandidateDecisionResult(ContractModel):
    contract_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    candidate_id: UUID
    state: CandidateLifecycleState
    outcome: MemoryOutcome
    operation_id: UUID | None = None
    memory_ids: tuple[UUID, ...] = ()
    rejection_code: MemoryRejectionCode | None = None
    message: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_candidate_decision(self) -> CandidateDecisionResult:
        if (
            self.state
            in {
                CandidateLifecycleState.NEEDS_REVIEW,
                CandidateLifecycleState.REJECTED,
            }
            and self.rejection_code is None
        ):
            raise ValueError("candidate_rejection_state_requires_code")
        if self.state is CandidateLifecycleState.APPLIED and self.operation_id is None:
            raise ValueError("applied_candidate_requires_operation_id")
        return self
