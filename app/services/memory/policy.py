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
        # Longer forms first so "court" is not shadowed by "ct" and "square" not
        # by "sq". The list previously stopped at boulevard, so a home address on
        # a Way, Court, Place or Crescent classified as NORMAL and could be
        # stored without the explicit request a home address is meant to require.
        r"(?:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd"
        r"|parkway|pkwy|court|ct|place|pl|terrace|terr|crescent|cres"
        r"|square|sq|close|way)\b",
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


# Anchored to the start of a sentence so it reads an instruction rather than a
# mention.  Unanchored, "what do you remember about my goals?" matched and every
# recall question was treated as a memory write.
_MEMORY_COMMAND = re.compile(
    r"^\s*(?:please\s+)?(?:remember|memorise|memorize|forget|save this|note that|call me)\b",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"\b(?:changed my mind|no longer|not anymore|instead of)\b",
    re.IGNORECASE,
)
# "me" is deliberately absent: it appears in requests for output ("give me a
# practice session") far more often than in statements about the user, and those
# requests state nothing to store.
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|ive|i'd|my|mine)\b", re.IGNORECASE)
_QUESTION_OPENER = re.compile(
    r"^\s*(?:what|who|when|where|why|which|how|do|does|did|is|are|was|were|can|could|"
    r"would|will|should|have|has|tell me|remind me|list|show)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def turn_may_contain_memory(text: str) -> bool:
    """Return whether a turn can plausibly contain a fact worth storing.

    Extraction used to run on every turn, so asking "what do you remember about
    my goals?" spent a local model call on a message that states nothing, and —
    because extraction also reads the supporting window — re-asserted facts from
    earlier messages as brand new candidates.  A question is the one shape that
    reliably carries nothing to store, so it is the only shape skipped here.

    The gate stays deliberately generous: anything with an explicit memory
    instruction runs, and so does any sentence the user speaks about themselves.
    Only a turn made entirely of questions is skipped, because a false negative
    silently loses a memory while a false positive merely costs what every turn
    already cost before this existed.
    """

    stripped = text.strip()
    if not stripped:
        return False
    for sentence in _SENTENCE_SPLIT.split(stripped):
        candidate = sentence.strip()
        if not candidate:
            continue
        if candidate.endswith("?") or _QUESTION_OPENER.match(candidate):
            continue
        if _MEMORY_COMMAND.match(candidate):
            return True
        if _CORRECTION.search(candidate) or _FIRST_PERSON.search(candidate):
            return True
    return False


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
