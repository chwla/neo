"""Versioned owner-bound contracts shared by every Phase 5 recall consumer."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.memory.contracts import Sensitivity
from app.services.memory.extraction_contracts import CurrentTurnOverride
from app.services.memory.taxonomy import MemoryType
from app.services.memory.versions import (
    LEXICAL_SCORING_VERSION,
    PROMPT_SERIALIZER_VERSION,
    RECALL_CONTRACT_VERSION,
    SEMANTIC_SCORING_VERSION,
)


class RecallContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class RecallMode(StrEnum):
    DETERMINISTIC = "deterministic"
    SCOPED_LEXICAL = "scoped_lexical"
    BROAD = "broad"


class RecallReasonCode(StrEnum):
    SELECTED = "selected"
    GATED_INCOGNITO = "gated_incognito"
    GATED_MEMORY_DISABLED = "gated_memory_disabled"
    OWNER_NOT_ENABLED = "owner_not_enabled"
    OWNER_DATABASE_MISMATCH = "owner_database_mismatch"
    INACTIVE = "inactive"
    EXPIRED = "expired"
    SENSITIVE_EXCLUDED = "sensitive_excluded"
    DOMAIN_FILTERED = "domain_filtered"
    BELOW_THRESHOLD = "below_threshold"
    DIVERSITY_DROPPED = "diversity_dropped"
    BUDGET_DROPPED = "budget_dropped"
    CURRENT_TURN_SUPPRESSED = "current_turn_suppressed"
    LEXICAL_UNAVAILABLE = "lexical_unavailable"
    NO_RELIABLE_MATCH = "no_reliable_match"
    SEMANTIC_UNAVAILABLE = "semantic_unavailable"


class MemoryQueryContext(RecallContractModel):
    contract_version: Literal[RECALL_CONTRACT_VERSION] = RECALL_CONTRACT_VERSION
    owner_id: UUID
    database_identity: str = Field(min_length=1, max_length=512)
    profile_id: str = Field(min_length=1, max_length=200)
    memory_enabled: bool = True
    incognito: bool = False
    request_id: str = Field(min_length=1, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    active_project_id: str | None = Field(default=None, max_length=80)
    current_time: datetime
    allowed_memory_types: frozenset[MemoryType] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    maximum_records: int = Field(default=5, ge=1, le=20)
    maximum_characters: int = Field(default=2_400, ge=200, le=12_000)
    mode: RecallMode
    current_turn_override: CurrentTurnOverride | None = None
    explicit_sensitive_lookup: bool = False
    lexical_available: bool = True

    @model_validator(mode="after")
    def validate_override_owner(self) -> MemoryQueryContext:
        if self.current_turn_override is not None and self.current_turn_override.owner_id != str(
            self.owner_id
        ):
            raise ValueError("current_turn_override_owner_mismatch")
        if self.explicit_sensitive_lookup and self.mode is not RecallMode.DETERMINISTIC:
            raise ValueError("sensitive_lookup_requires_deterministic_mode")
        return self


class CanonicalMemoryView(RecallContractModel):
    canonical_id: UUID
    owner_id: UUID
    memory_type: MemoryType
    scope_type: str = "global"
    scope_project_id: str | None = None
    domain_key: str
    slot_key: str
    display_text: str
    sensitivity: Sensitivity
    confidence: float = Field(ge=0, le=1)
    importance: int = Field(ge=1, le=10)
    pinned: bool
    usage_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    last_confirmed_at: datetime
    last_used_at: datetime | None = None
    source_ids: tuple[UUID, ...] = ()


class RecallQuery(RecallContractModel):
    context: MemoryQueryContext
    text: str = Field(default="", max_length=12_000)
    canonical_id: UUID | None = None
    memory_type: MemoryType | None = None
    domain_key: str | None = Field(default=None, max_length=200)
    slot_key: str | None = Field(default=None, max_length=400)
    trusted_slot_keys: tuple[str, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def deterministic_selector_required(self) -> RecallQuery:
        if self.context.mode is RecallMode.DETERMINISTIC and not any(
            (self.canonical_id, self.slot_key, self.trusted_slot_keys, self.memory_type)
        ):
            raise ValueError("deterministic_recall_selector_required")
        return self


class RecallScoreBreakdown(RecallContractModel):
    scoring_policy_version: Literal[LEXICAL_SCORING_VERSION] = LEXICAL_SCORING_VERSION
    domain_fit: float = Field(ge=0, le=1)
    lexical: float = Field(ge=0, le=1)
    semantic: float = Field(default=0, ge=0, le=1)
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    confirmation_freshness: float = Field(ge=0, le=1)
    recency: float = Field(ge=0, le=1)
    usage: float = Field(ge=0, le=1)
    pin: float = Field(ge=0, le=1)
    total: float = Field(ge=0, le=1)


class RecallCandidate(RecallContractModel):
    memory: CanonicalMemoryView
    score: RecallScoreBreakdown
    reason_codes: tuple[RecallReasonCode, ...] = ()


class RecallItem(RecallContractModel):
    memory: CanonicalMemoryView
    score: RecallScoreBreakdown
    reason_code: RecallReasonCode = RecallReasonCode.SELECTED


class RecallScoreDiagnostic(RecallContractModel):
    canonical_id: UUID
    score: RecallScoreBreakdown


class RecallDiagnostic(RecallContractModel):
    owner_database_binding: str
    recall_mode: RecallMode
    eligible_candidate_count: int = Field(ge=0)
    filtered_inactive_count: int = Field(default=0, ge=0)
    filtered_expired_count: int = Field(default=0, ge=0)
    filtered_sensitivity_count: int = Field(default=0, ge=0)
    domain_filtered_count: int = Field(default=0, ge=0)
    below_threshold_count: int = Field(default=0, ge=0)
    diversity_dropped_count: int = Field(default=0, ge=0)
    budget_dropped_count: int = Field(default=0, ge=0)
    suppressed_ids: tuple[UUID, ...] = ()
    final_injected_ids: tuple[UUID, ...] = ()
    usage_event_ids: tuple[UUID, ...] = ()
    degraded_lexical: bool = False
    semantic_candidate_count: int = Field(default=0, ge=0)
    semantic_validated_count: int = Field(default=0, ge=0)
    semantic_stale_drop_count: int = Field(default=0, ge=0)
    semantic_ghost_drop_count: int = Field(default=0, ge=0)
    semantic_wrong_owner_drop_count: int = Field(default=0, ge=0)
    semantic_inactive_drop_count: int = Field(default=0, ge=0)
    semantic_repair_count: int = Field(default=0, ge=0)
    degraded_semantic_reason: str | None = Field(default=None, max_length=80)
    score_components: tuple[RecallScoreDiagnostic, ...] = ()
    latency_ms: int = Field(default=0, ge=0)
    scoring_policy_version: Literal[LEXICAL_SCORING_VERSION] = LEXICAL_SCORING_VERSION
    prompt_serializer_version: Literal[PROMPT_SERIALIZER_VERSION] = PROMPT_SERIALIZER_VERSION
    semantic_scoring_version: Literal[SEMANTIC_SCORING_VERSION] = SEMANTIC_SCORING_VERSION
    reason_codes: tuple[RecallReasonCode, ...] = ()


class RecallResult(RecallContractModel):
    contract_version: Literal[RECALL_CONTRACT_VERSION] = RECALL_CONTRACT_VERSION
    mode: RecallMode
    items: tuple[RecallItem, ...]
    diagnostic: RecallDiagnostic

    @property
    def canonical_ids(self) -> tuple[UUID, ...]:
        return tuple(item.memory.canonical_id for item in self.items)


class UsageSelection(RecallContractModel):
    owner_id: UUID
    request_id: str
    session_id: str | None = None
    purpose: str = Field(min_length=1, max_length=80)
    canonical_ids: tuple[UUID, ...]
    selected_at: datetime


class SerializedMemoryContext(RecallContractModel):
    serializer_version: Literal[PROMPT_SERIALIZER_VERSION] = PROMPT_SERIALIZER_VERSION
    role: Literal["user"] = "user"
    name: Literal["neo_untrusted_memory_context"] = "neo_untrusted_memory_context"
    content: str
    canonical_ids: tuple[UUID, ...]
    character_count: int = Field(ge=0)


class RecallPromptSelection(RecallContractModel):
    recall: RecallResult
    serialized: SerializedMemoryContext | None
    usage: UsageSelection | None
    usage_recorded: bool
    usage_failure_code: str | None = Field(default=None, max_length=120)
