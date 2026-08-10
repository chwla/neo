"""Approved Phase 0 product policy for Neo personal memory memory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from app.services.memory.contracts import (
    CandidateProposal,
    MemoryCommandBase,
    MemoryCommandResult,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    Sensitivity,
)
from app.services.memory.versions import POLICY_VERSION

MEMORY_POLICY_VERSION = POLICY_VERSION


FORGET_TOMBSTONE_DAYS = 30
MAX_RECALL_RECORDS = 5
MAX_RECALL_CONTEXT_CHARS = 2_400
MAX_AUTOMATIC_CANDIDATES_PER_TURN = 4
MAX_EXPLICIT_CANDIDATES_PER_BATCH = 50
USAGE_AFFECTS_RANKING = False
CURRENT_USER_MESSAGE_OVERRIDES_STORED_CONTEXT = True


class GuestStoreKind(StrEnum):
    REGISTERED_PROFILE = "registered_profile"
    EPHEMERAL_PROFILE = "ephemeral_profile"


class ConflictEvidence(StrEnum):
    EXPLICIT_CORRECTION = "explicit_correction"
    LINKED_RETRACTION_REPLACEMENT = "linked_retraction_replacement"
    GROUNDED_SAME_SLOT_ASSERTION = "grounded_same_slot_assertion"
    UNMARKED_INCOMPATIBLE_ASSERTION = "unmarked_incompatible_assertion"


class ConflictAction(StrEnum):
    APPLY_REPLACEMENT = "apply_replacement"
    NEEDS_REVIEW = "needs_review"


class ExtractionTrigger(StrEnum):
    EXPLICIT_MEMORY_COMMAND = "explicit_memory_command"
    DETERMINISTIC_CORRECTION = "deterministic_correction"
    AUTOMATIC_LLM = "automatic_llm"


class ExtractionTiming(StrEnum):
    BEFORE_RESPONSE = "before_response"
    AFTER_TURN = "after_turn"


@dataclass(frozen=True)
class MemoryExecutionContext:
    memory_enabled: bool = True
    incognito: bool = False
    is_guest: bool = False


@dataclass(frozen=True)
class CandidatePolicyDecision:
    allowed: bool
    sensitivity: Sensitivity
    rejection_code: MemoryRejectionCode | None = None


@dataclass(frozen=True)
class ConflictPolicyDecision:
    action: ConflictAction
    outcome: MemoryOutcome
    rejection_code: MemoryRejectionCode | None
    existing_record_stays_active: bool


@dataclass(frozen=True)
class ExtractionTimingDecision:
    timing: ExtractionTiming
    use_current_turn_overlay: bool


@dataclass(frozen=True)
class DeletionPolicy:
    operation: MemoryOperationKind
    remove_recallable_content: bool
    remove_derived_indexes: bool
    remove_provenance: bool
    remove_eligible_audit_content: bool
    retain_fingerprint_tombstone_days: int
    automatic_resurrection_blocked: bool
    explicit_reconfirmation_allowed: bool


@dataclass(frozen=True)
class PinPolicy:
    ranking_boost_only: bool = True
    max_score_boost: float = 0.05
    guarantees_inclusion: bool = False
    bypasses_owner: bool = False
    bypasses_status: bool = False
    bypasses_expiry: bool = False
    bypasses_sensitivity: bool = False
    bypasses_relevance: bool = False
    bypasses_domain: bool = False
    bypasses_token_budget: bool = False
    bypasses_recall_limit: bool = False


PIN_POLICY = PinPolicy()


_PROHIBITED_PATTERNS = (
    re.compile(r"\bpassword\b\s*(?:is|[:=])\s*\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:otp|one[- ]time password|verification code)\b"
        r"\s*(?:is|[:=])?\s*\d{4,8}\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:api[ _-]?key|access[ _-]?token|authentication[ _-]?secret|"
        r"auth[ _-]?secret|client[ _-]?secret)\b\s*(?:is|[:=])\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
)

_SENSITIVE_PATTERNS = (
    # The "i have" branch is a health disclosure, so it must be followed by
    # something medical.  Left bare it matched every ordinary "I have ..."
    # sentence ("I have two cats"), classifying routine facts as sensitive and
    # silently refusing to store them without an explicit request.
    re.compile(
        r"\b(?:my diagnosis|i was diagnosed with|my medical condition|"
        r"my medication|i take medication|my mental health|"
        r"i have\s+(?:\w+\s+){0,3}?(?:cancer|diabetes|asthma|adhd|autism|"
        r"depression|anxiety|epilepsy|hiv|aids|migraines?|arthritis|"
        r"disorder|disease|syndrome|condition|illness|allerg(?:y|ies)|"
        r"\w+(?:itis|osis|emia|opathy)))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my )?(?:aadhaar|aadhar|social security|ssn|passport|"
        r"driver'?s licen[cs]e)\b(?: number)?\s*(?:is|[:=])\s*[A-Z0-9 -]{4,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my )?(?:bank account|financial account|routing number|iban)\b"
        r"(?: number)?\s*(?:is|[:=])\s*[A-Z0-9 -]{4,}",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:my (?:home |private )?address|i live)\s*(?:is|at|[:=])\s*"
        r"\d{1,6}\s+[A-Za-z0-9 .'-]+\s+"
        r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd)\b",
        re.IGNORECASE,
    ),
)


def _luhn_valid(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _contains_complete_card_number(text: str) -> bool:
    candidates = re.findall(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)", text)
    return any(_luhn_valid(candidate) for candidate in candidates)


def classify_sensitivity(text: str) -> Sensitivity:
    if any(pattern.search(text) for pattern in _PROHIBITED_PATTERNS):
        return Sensitivity.PROHIBITED
    if _contains_complete_card_number(text):
        return Sensitivity.PROHIBITED
    if any(pattern.search(text) for pattern in _SENSITIVE_PATTERNS):
        return Sensitivity.SENSITIVE
    return Sensitivity.NORMAL


def candidate_persistence_decision(proposal: CandidateProposal) -> CandidatePolicyDecision:
    candidate_text = f"{proposal.display_text}\n{json.dumps(proposal.canonical_value)}"
    detected = classify_sensitivity(candidate_text)
    order = {Sensitivity.NORMAL: 0, Sensitivity.SENSITIVE: 1, Sensitivity.PROHIBITED: 2}
    sensitivity = max((proposal.sensitivity, detected), key=order.__getitem__)

    if sensitivity is Sensitivity.PROHIBITED:
        return CandidatePolicyDecision(
            allowed=False,
            sensitivity=sensitivity,
            rejection_code=MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT,
        )
    if sensitivity is Sensitivity.SENSITIVE and not proposal.explicit_user_request:
        return CandidatePolicyDecision(
            allowed=False,
            sensitivity=sensitivity,
            rejection_code=MemoryRejectionCode.SENSITIVE_REQUIRES_EXPLICIT_REQUEST,
        )
    return CandidatePolicyDecision(allowed=True, sensitivity=sensitivity)


def can_recall_sensitivity(
    sensitivity: Sensitivity,
    *,
    directly_relevant: bool,
) -> bool:
    if sensitivity is Sensitivity.PROHIBITED:
        return False
    if sensitivity is Sensitivity.SENSITIVE:
        return directly_relevant
    return True


def deletion_policy(operation: MemoryOperationKind) -> DeletionPolicy:
    if operation is MemoryOperationKind.FORGET:
        return DeletionPolicy(
            operation=operation,
            remove_recallable_content=True,
            remove_derived_indexes=True,
            remove_provenance=False,
            remove_eligible_audit_content=False,
            retain_fingerprint_tombstone_days=FORGET_TOMBSTONE_DAYS,
            automatic_resurrection_blocked=True,
            explicit_reconfirmation_allowed=True,
        )
    if operation is MemoryOperationKind.ERASE_PERMANENTLY:
        return DeletionPolicy(
            operation=operation,
            remove_recallable_content=True,
            remove_derived_indexes=True,
            remove_provenance=True,
            remove_eligible_audit_content=True,
            retain_fingerprint_tombstone_days=0,
            automatic_resurrection_blocked=False,
            explicit_reconfirmation_allowed=True,
        )
    raise ValueError("deletion_policy_requires_forget_or_erase_permanently")


def conflict_policy_decision(
    evidence: ConflictEvidence,
    *,
    target_relationship_deterministic: bool,
) -> ConflictPolicyDecision:
    automatic_evidence = {
        ConflictEvidence.EXPLICIT_CORRECTION,
        ConflictEvidence.LINKED_RETRACTION_REPLACEMENT,
        ConflictEvidence.GROUNDED_SAME_SLOT_ASSERTION,
    }
    if evidence in automatic_evidence and target_relationship_deterministic:
        return ConflictPolicyDecision(
            action=ConflictAction.APPLY_REPLACEMENT,
            outcome=MemoryOutcome.REPLACED,
            rejection_code=None,
            existing_record_stays_active=False,
        )
    return ConflictPolicyDecision(
        action=ConflictAction.NEEDS_REVIEW,
        outcome=MemoryOutcome.NEEDS_REVIEW,
        rejection_code=MemoryRejectionCode.AMBIGUOUS_CONFLICT,
        existing_record_stays_active=True,
    )


def extraction_timing_policy(trigger: ExtractionTrigger) -> ExtractionTimingDecision:
    if trigger in {
        ExtractionTrigger.EXPLICIT_MEMORY_COMMAND,
        ExtractionTrigger.DETERMINISTIC_CORRECTION,
    }:
        return ExtractionTimingDecision(
            timing=ExtractionTiming.BEFORE_RESPONSE,
            use_current_turn_overlay=trigger is ExtractionTrigger.DETERMINISTIC_CORRECTION,
        )
    return ExtractionTimingDecision(
        timing=ExtractionTiming.AFTER_TURN,
        use_current_turn_overlay=False,
    )


def guest_store_kind(*, is_guest: bool) -> GuestStoreKind:
    return GuestStoreKind.EPHEMERAL_PROFILE if is_guest else GuestStoreKind.REGISTERED_PROFILE


def gate_memory_command(
    command: MemoryCommandBase,
    context: MemoryExecutionContext,
) -> MemoryCommandResult | None:
    if context.incognito:
        return MemoryCommandResult.disabled_for(
            command,
            rejection_code=MemoryRejectionCode.INCOGNITO_DISABLED,
            message="Personal memory is disabled in incognito mode.",
        )
    if not context.memory_enabled:
        return MemoryCommandResult.disabled_for(
            command,
            rejection_code=MemoryRejectionCode.MEMORY_DISABLED,
            message="Personal memory is disabled.",
        )
    return None
