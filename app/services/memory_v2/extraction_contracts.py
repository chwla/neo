"""Versioned, SQLAlchemy-free contracts for Phase 4 extraction and planning."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.services.memory_v2.contracts import (
    CandidateGroundingSpan,
    OwnerId,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory_v2.versions import POLICY_VERSION, TAXONOMY_VERSION

EXTRACTION_CONTRACT_VERSION = "neo.memory.extraction.v1"
MODEL_SCHEMA_VERSION = 1
DEFAULT_AUTOMATIC_CANDIDATE_LIMIT = 4
MAX_EXPLICIT_BATCH_CANDIDATES = 50


class ExtractionContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ExtractionMode(StrEnum):
    FOREGROUND_DETERMINISTIC = "foreground_deterministic"
    POST_TURN_AUTOMATIC = "post_turn_automatic"
    EXPLICIT_BATCH = "explicit_batch"
    SUGGESTION_ONLY = "suggestion_only"


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ExtractionStatus(StrEnum):
    APPLIED = "applied"
    NO_ACTION = "no_action"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"
    FAILED = "failed"
    DISABLED = "disabled"
    DEFERRED = "deferred"


class CandidateAction(StrEnum):
    CREATE = "create"
    RECONFIRM = "reconfirm"
    REFINE = "refine"
    REPLACE = "replace"
    RETRACT = "retract"
    FORGET = "forget"
    REVIEW = "review"
    REJECT = "reject"
    IGNORE = "ignore"
    IDEMPOTENT_REPLAY = "idempotent_replay"


class PreparseKind(StrEnum):
    DETERMINISTIC_ASSERTION = "deterministic_assertion"
    DETERMINISTIC_CORRECTION = "deterministic_correction"
    EXPLICIT_LIFECYCLE = "explicit_lifecycle"
    EXPLICIT_BATCH = "explicit_batch"
    MODEL_REQUIRED = "model_required"
    IGNORE = "ignore"
    AMBIGUOUS = "ambiguous"


class LifecycleHint(StrEnum):
    ARCHIVE = "archive"
    FORGET = "forget"
    ERASE_PERMANENTLY = "erase_permanently"
    RESTORE = "restore"


class TrustedConversationMessage(ExtractionContractModel):
    message_id: str = Field(min_length=1, max_length=200)
    role: ConversationRole
    content: str = Field(min_length=1, max_length=12_000)


class ExtractionRequest(ExtractionContractModel):
    contract_version: Literal[EXTRACTION_CONTRACT_VERSION] = EXTRACTION_CONTRACT_VERSION
    taxonomy_version: Literal[TAXONOMY_VERSION] = TAXONOMY_VERSION
    policy_version: Literal[POLICY_VERSION] = POLICY_VERSION
    request_id: str = Field(min_length=1, max_length=200)
    owner_id: OwnerId
    conversation_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    user_message: str = Field(min_length=1, max_length=12_000)
    supporting_window: tuple[TrustedConversationMessage, ...] = Field(default=(), max_length=12)
    explicit_memory_intent: bool = False
    incognito: bool = False
    memory_enabled: bool = True
    mode: ExtractionMode
    maximum_candidates: int = Field(default=DEFAULT_AUTOMATIC_CANDIDATE_LIMIT, ge=1, le=50)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @staticmethod
    def content_hash(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @model_validator(mode="after")
    def validate_trusted_request(self) -> ExtractionRequest:
        if self.source_content_hash != self.content_hash(self.user_message):
            raise ValueError("source_content_hash_mismatch")
        if self.mode is ExtractionMode.POST_TURN_AUTOMATIC and self.maximum_candidates > 4:
            raise ValueError("automatic_candidate_limit_exceeds_policy")
        if self.mode is not ExtractionMode.EXPLICIT_BATCH and self.maximum_candidates > 4:
            raise ValueError("non_batch_candidate_limit_exceeds_policy")
        message_ids = [item.message_id for item in self.supporting_window]
        if self.message_id in message_ids or len(message_ids) != len(set(message_ids)):
            raise ValueError("conversation_message_ids_must_be_unique")
        total_chars = len(self.user_message) + sum(
            len(item.content) for item in self.supporting_window
        )
        if total_chars > 12_000:
            raise ValueError("bounded_conversation_window_exceeded")
        return self

    def authorized_user_messages(self) -> dict[str, str]:
        messages = {self.message_id: self.user_message}
        messages.update(
            {
                item.message_id: item.content
                for item in self.supporting_window
                if item.role is ConversationRole.USER
            }
        )
        return messages


class ModelSourceMessage(ExtractionContractModel):
    message_id: str = Field(min_length=1, max_length=200)
    role: ConversationRole
    content: str = Field(min_length=1, max_length=12_000)


class ModelExtractionInput(ExtractionContractModel):
    schema_version: Literal[MODEL_SCHEMA_VERSION] = MODEL_SCHEMA_VERSION
    request_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    user_message: str = Field(min_length=1, max_length=12_000)
    supporting_window: tuple[ModelSourceMessage, ...] = ()
    explicit_memory_intent: bool
    mode: ExtractionMode
    maximum_candidates: int = Field(ge=1, le=50)

    @classmethod
    def from_trusted_request(cls, request: ExtractionRequest) -> ModelExtractionInput:
        return cls(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            message_id=request.message_id,
            user_message=request.user_message,
            supporting_window=tuple(
                ModelSourceMessage(
                    message_id=item.message_id,
                    role=item.role,
                    content=item.content,
                )
                for item in request.supporting_window
                if item.role in {ConversationRole.USER, ConversationRole.ASSISTANT}
            ),
            explicit_memory_intent=request.explicit_memory_intent,
            mode=request.mode,
            maximum_candidates=request.maximum_candidates,
        )


class PreparsedSourceSpan(ExtractionContractModel):
    message_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quoted_text: str = Field(min_length=1, max_length=4_000)

    @model_validator(mode="after")
    def validate_offsets(self) -> PreparsedSourceSpan:
        if self.end <= self.start:
            raise ValueError("span_end_must_follow_start")
        return self


class PreparsedSpan(ExtractionContractModel):
    message_id: str
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    quoted_text: str = Field(min_length=1)
    normalized_value: str = Field(min_length=1)
    memory_type_hint: str | None = None
    domain_hint: str | None = None
    slot_hint: str | None = None
    correction_group: str | None = None
    additive: bool = False
    additional_source_spans: tuple[PreparsedSourceSpan, ...] = Field(default=(), max_length=3)
    explicit_type_change: bool = False
    explicit_domain_change: bool = False
    explicit_slot_change: bool = False

    @model_validator(mode="after")
    def validate_offsets(self) -> PreparsedSpan:
        if self.end <= self.start:
            raise ValueError("span_end_must_follow_start")
        return self


class PreparseResult(ExtractionContractModel):
    kind: PreparseKind
    reason: str = Field(min_length=1)
    assertions: tuple[PreparsedSpan, ...] = ()
    retractions: tuple[PreparsedSpan, ...] = ()
    lifecycle_hint: LifecycleHint | None = None
    explicit_memory_intent: bool = False
    deterministic: bool = False
    hypothetical: bool = False
    temporary: bool = False
    third_party: bool = False
    question: bool = False
    ambiguous_subject: bool = False
    sensitive_content_redacted: bool = False


class GroundingDecision(ExtractionContractModel):
    proposal_id: str
    accepted: bool
    reason: str
    spans: tuple[CandidateGroundingSpan, ...] = ()


class NormalizedExtractionCandidate(ExtractionContractModel):
    proposal: ValidatedCandidateProposal
    grounding_spans: tuple[CandidateGroundingSpan, ...] = Field(min_length=1)
    correction_group: str | None = None
    old_value_hints: tuple[str, ...] = ()
    explicit_type_change: bool = False
    explicit_domain_change: bool = False
    explicit_slot_change: bool = False
    expires_at: datetime | None = None


class CurrentTurnOverride(ExtractionContractModel):
    owner_id: OwnerId
    source_message_id: str
    suppressed_memory_ids: tuple[UUID, ...] = ()
    suppressed_slot_keys: tuple[str, ...] = ()
    candidate_target_memory_ids: tuple[UUID, ...] = ()
    unresolved_conflict_slot_keys: tuple[str, ...] = ()
    positive_current_assertion: JsonValue | None = None
    redacted_current_assertion: str | None = None
    sensitivity: Sensitivity = Sensitivity.NORMAL
    review_required: bool = False
    # Compatibility aliases. They are always identical to the explicit suppression fields.
    contradicted_memory_ids: tuple[UUID, ...] = ()
    contradicted_slot_keys: tuple[str, ...] = ()
    unresolved_target_hints: tuple[str, ...] = ()
    confidence: float = Field(default=0.0, ge=0, le=1)
    contradiction_deterministic: bool = False

    @model_validator(mode="after")
    def validate_suppression_authority(self) -> CurrentTurnOverride:
        if self.contradicted_memory_ids != self.suppressed_memory_ids:
            raise ValueError("contradicted_ids_must_alias_explicit_suppression")
        if self.contradicted_slot_keys != self.suppressed_slot_keys:
            raise ValueError("contradicted_slots_must_alias_explicit_suppression")
        if self.suppressed_memory_ids and not self.contradiction_deterministic:
            raise ValueError("suppression_requires_deterministic_contradiction")
        if self.review_required and self.suppressed_memory_ids:
            raise ValueError("review_ambiguity_cannot_authorize_suppression")
        if self.positive_current_assertion is not None and self.redacted_current_assertion:
            raise ValueError("current_assertion_cannot_be_plaintext_and_redacted")
        if (
            self.sensitivity is not Sensitivity.NORMAL
            and self.positive_current_assertion is not None
        ):
            raise ValueError("sensitive_current_assertion_cannot_expose_plaintext")
        if self.sensitivity is Sensitivity.PROHIBITED:
            if any(
                (
                    self.suppressed_memory_ids,
                    self.suppressed_slot_keys,
                    self.candidate_target_memory_ids,
                    self.unresolved_conflict_slot_keys,
                    self.unresolved_target_hints,
                )
            ):
                raise ValueError("prohibited_override_must_be_empty")
            if self.redacted_current_assertion is not None:
                raise ValueError("prohibited_override_cannot_expose_redacted_value")
        return self


class ExtractionCandidateDecision(ExtractionContractModel):
    candidate_id: UUID | None = None
    action: CandidateAction
    reason: str
    review_required: bool = False
    memory_ids: tuple[UUID, ...] = ()
    operation_id: UUID | None = None
    outcome: str | None = None
    proposed_memory_type: str | None = None
    proposed_domain_hint: str | None = None
    proposed_slot_hint: str | None = None
    domain_unresolved: bool = False
    slot_unresolved: bool = False
    model_failure_reason: str | None = None


class ModelProposalSummary(ExtractionContractModel):
    called: bool
    assertion_count: int = Field(ge=0)
    retraction_count: int = Field(ge=0)
    exclusion_count: int = Field(ge=0)
    capped_count: int = Field(default=0, ge=0)
    raw_output_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ExtractionDiagnostic(ExtractionContractModel):
    request_id: str
    owner_id: OwnerId
    message_id: str
    extractor_version: str
    model_version: str | None = None
    prompt_version: str | None = None
    model_latency_ms: int | None = Field(default=None, ge=0)
    provider_kind: str | None = Field(default=None, max_length=80)
    http_status: int | None = Field(default=None, ge=100, le=599)
    response_envelope_shape: str | None = Field(default=None, max_length=120)
    content_present: bool | None = None
    content_byte_length: int | None = Field(default=None, ge=0, le=128_000)
    response_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sanitized_provider_error_code: str | None = Field(default=None, max_length=120)
    provider_timeout_stage: str | None = Field(default=None, max_length=40)
    json_parse_result: str | None = Field(default=None, max_length=80)
    schema_validation_result: str | None = Field(default=None, max_length=80)
    schema_error_codes: tuple[str, ...] = Field(default=(), max_length=20)
    parse_outcome: str
    proposal_count: int = Field(ge=0)
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    reason_codes: tuple[str, ...] = ()
    operation_ids: tuple[UUID, ...] = ()
    suppressed_memory_ids: tuple[UUID, ...] = ()
    candidate_target_memory_ids: tuple[UUID, ...] = ()
    unresolved_conflict_slot_keys: tuple[str, ...] = ()


class ExtractionResult(ExtractionContractModel):
    request_id: str
    owner_id: OwnerId
    message_id: str
    status: ExtractionStatus
    timing: str
    preparse: PreparseResult
    model_summary: ModelProposalSummary
    grounding: tuple[GroundingDecision, ...] = ()
    decisions: tuple[ExtractionCandidateDecision, ...] = ()
    current_turn_override: CurrentTurnOverride
    diagnostic: ExtractionDiagnostic
