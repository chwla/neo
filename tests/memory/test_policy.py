from __future__ import annotations

from uuid import UUID

import pytest

from app.services.memory.contracts import (
    CandidateProposal,
    CreateMemoryCommand,
    MemoryActor,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MemorySource,
    Sensitivity,
    ValidatedCandidateProposal,
)
from app.services.memory.policy import (
    CURRENT_USER_MESSAGE_OVERRIDES_STORED_CONTEXT,
    FORGET_TOMBSTONE_DAYS,
    MAX_AUTOMATIC_CANDIDATES_PER_TURN,
    MAX_EXPLICIT_CANDIDATES_PER_BATCH,
    MAX_RECALL_CONTEXT_CHARS,
    MAX_RECALL_RECORDS,
    MEMORY_POLICY_VERSION,
    PIN_POLICY,
    USAGE_AFFECTS_RANKING,
    ConflictAction,
    ConflictEvidence,
    ExtractionTiming,
    ExtractionTrigger,
    GuestStoreKind,
    MemoryExecutionContext,
    can_recall_sensitivity,
    candidate_persistence_decision,
    classify_sensitivity,
    conflict_policy_decision,
    deletion_policy,
    extraction_timing_policy,
    gate_memory_command,
    guest_store_kind,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from app.services.memory.versions import POLICY_VERSION


@pytest.mark.parametrize(
    "text",
    [
        "My password is hunter2!",
        "My OTP is 123456",
        "My API key is sk-abcdefghijklmnop",
        "My access token: abcdefghijklmnop",
        "-----BEGIN PRIVATE KEY-----",
        "My card is 4242 4242 4242 4242",
    ],
)
def test_prohibited_secret_classification(text: str) -> None:
    assert classify_sensitivity(text) is Sensitivity.PROHIBITED


@pytest.mark.parametrize(
    "text",
    [
        "I was diagnosed with asthma.",
        "My passport number is A1234567.",
        "My bank account number is 123456789.",
        "My home address is 123 Main Street.",
    ],
)
def test_sensitive_personal_fact_classification(text: str) -> None:
    assert classify_sensitivity(text) is Sensitivity.SENSITIVE


def test_prohibited_secret_cannot_pass_candidate_persistence_policy() -> None:
    proposal = CandidateProposal(
        proposal_id=UUID("00000000-0000-4000-8000-000000000401"),
        memory_type=MemoryType.KNOWLEDGE,
        domain_key="software_development",
        slot_key="knowledge:software_development:item:secret",
        cardinality=Cardinality.ADDITIVE,
        canonical_value="sk-abcdefghijklmnop",
        display_text="My API key is sk-abcdefghijklmnop",
        sensitivity=Sensitivity.NORMAL,
        confidence=1,
        importance=10,
        explicit_user_request=True,
    )
    decision = candidate_persistence_decision(proposal)
    assert not decision.allowed
    assert decision.sensitivity is Sensitivity.PROHIBITED
    assert decision.rejection_code is MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT


def test_sensitive_fact_requires_explicit_user_request() -> None:
    proposal = CandidateProposal(
        proposal_id=UUID("00000000-0000-4000-8000-000000000402"),
        memory_type=MemoryType.IDENTITY,
        domain_key="global",
        slot_key="identity:global:home_address",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value="123 Main Street",
        display_text="My home address is 123 Main Street",
        sensitivity=Sensitivity.SENSITIVE,
        confidence=0.99,
        importance=7,
    )
    automatic = candidate_persistence_decision(proposal)
    assert automatic.rejection_code is MemoryRejectionCode.SENSITIVE_REQUIRES_EXPLICIT_REQUEST

    explicit_payload = proposal.model_dump()
    explicit_payload["explicit_user_request"] = True
    explicit = candidate_persistence_decision(CandidateProposal.model_validate(explicit_payload))
    assert explicit.allowed
    assert explicit.sensitivity is Sensitivity.SENSITIVE


def test_sensitive_recall_requires_direct_relevance() -> None:
    assert not can_recall_sensitivity(Sensitivity.SENSITIVE, directly_relevant=False)
    assert can_recall_sensitivity(Sensitivity.SENSITIVE, directly_relevant=True)
    assert not can_recall_sensitivity(Sensitivity.PROHIBITED, directly_relevant=True)


def test_forget_and_erase_have_distinct_retention() -> None:
    forget = deletion_policy(MemoryOperationKind.FORGET)
    erase = deletion_policy(MemoryOperationKind.ERASE_PERMANENTLY)

    assert forget.retain_fingerprint_tombstone_days == FORGET_TOMBSTONE_DAYS == 30
    assert forget.automatic_resurrection_blocked
    assert not forget.remove_provenance
    assert erase.retain_fingerprint_tombstone_days == 0
    assert erase.remove_provenance
    assert erase.remove_eligible_audit_content
    assert not erase.automatic_resurrection_blocked


def test_ambiguous_conflict_needs_review_and_keeps_existing_active() -> None:
    decision = conflict_policy_decision(
        ConflictEvidence.UNMARKED_INCOMPATIBLE_ASSERTION,
        target_relationship_deterministic=False,
    )
    assert decision.action is ConflictAction.NEEDS_REVIEW
    assert decision.outcome is MemoryOutcome.NEEDS_REVIEW
    assert decision.existing_record_stays_active
    assert decision.rejection_code is MemoryRejectionCode.AMBIGUOUS_CONFLICT


@pytest.mark.parametrize(
    "evidence",
    [
        ConflictEvidence.EXPLICIT_CORRECTION,
        ConflictEvidence.LINKED_RETRACTION_REPLACEMENT,
        ConflictEvidence.GROUNDED_SAME_SLOT_ASSERTION,
    ],
)
def test_deterministic_corrections_apply_without_newest_wins(
    evidence: ConflictEvidence,
) -> None:
    decision = conflict_policy_decision(evidence, target_relationship_deterministic=True)
    assert decision.action is ConflictAction.APPLY_REPLACEMENT
    assert not decision.existing_record_stays_active


def test_pin_policy_is_only_a_bounded_boost() -> None:
    assert PIN_POLICY.ranking_boost_only
    assert 0 < PIN_POLICY.max_score_boost < 1
    assert not PIN_POLICY.guarantees_inclusion
    assert not any(
        (
            PIN_POLICY.bypasses_owner,
            PIN_POLICY.bypasses_status,
            PIN_POLICY.bypasses_expiry,
            PIN_POLICY.bypasses_sensitivity,
            PIN_POLICY.bypasses_relevance,
            PIN_POLICY.bypasses_domain,
            PIN_POLICY.bypasses_token_budget,
            PIN_POLICY.bypasses_recall_limit,
        )
    )


def test_incognito_command_returns_disabled_result(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    command = CreateMemoryCommand(
        owner_id="00000000-0000-4000-8000-000000000001",
        idempotency_key="incognito-command-1",
        actor=user_actor,
        source=direct_source,
        candidate=normal_goal_candidate,
    )
    result = gate_memory_command(command, MemoryExecutionContext(incognito=True))
    assert result is not None
    assert result.outcome is MemoryOutcome.DISABLED
    assert result.rejection_code is MemoryRejectionCode.INCOGNITO_DISABLED
    assert result.affected_memory_ids == ()
    assert result.active_memory_ids == ()


def test_extraction_timing_and_overlay_policy() -> None:
    explicit = extraction_timing_policy(ExtractionTrigger.EXPLICIT_MEMORY_COMMAND)
    correction = extraction_timing_policy(ExtractionTrigger.DETERMINISTIC_CORRECTION)
    automatic = extraction_timing_policy(ExtractionTrigger.AUTOMATIC_LLM)
    assert explicit.timing is ExtractionTiming.BEFORE_RESPONSE
    assert correction.timing is ExtractionTiming.BEFORE_RESPONSE
    assert correction.use_current_turn_overlay
    assert automatic.timing is ExtractionTiming.AFTER_TURN
    assert not automatic.use_current_turn_overlay


def test_guest_usage_and_limits_are_frozen_for_v1() -> None:
    assert guest_store_kind(is_guest=True) is GuestStoreKind.EPHEMERAL_PROFILE
    assert guest_store_kind(is_guest=False) is GuestStoreKind.REGISTERED_PROFILE
    assert MAX_RECALL_RECORDS == 5
    assert MAX_RECALL_CONTEXT_CHARS == 2_400
    assert MAX_AUTOMATIC_CANDIDATES_PER_TURN == 4
    assert MAX_EXPLICIT_CANDIDATES_PER_BATCH == 50
    assert not USAGE_AFFECTS_RANKING
    assert CURRENT_USER_MESSAGE_OVERRIDES_STORED_CONTEXT
    assert MEMORY_POLICY_VERSION == POLICY_VERSION
