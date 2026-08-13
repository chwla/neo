"""Tier 1 — product policy (plan section POL).

Policy answers three questions that decide whether Neo is safe to leave running:
should this turn be looked at for facts at all, is this content something we are
allowed to keep, and what happens when a new fact contradicts an old one.

Two of the patterns here carry scars.  The extraction gate exists because
extraction used to run on every turn, so asking "what do you remember about my
goals?" re-asserted three earlier facts as brand-new memories.  The health
branch of the sensitive classifier is narrowed because a bare "I have ..." once
matched "I have two cats" and quietly refused to store ordinary facts.
"""

from __future__ import annotations

import pytest

from app.services.memory.contracts import (
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    Sensitivity,
)
from app.services.memory.policy import (
    FORGET_TOMBSTONE_DAYS,
    PIN_POLICY,
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
    turn_may_contain_memory,
)
from tests.memory import factories


class TestExtractionGateVerbs:
    @pytest.mark.parametrize(
        "verb",
        [
            "Remember",
            "Memorise",
            "Memorize",
            "Forget",
            "Save this",
            "Note that",
            "Call me",
        ],
    )
    def test_each_memory_verb_opens_the_gate(self, verb: str) -> None:
        """POL-05a — every verb we advertise in the pattern actually works."""

        assert turn_may_contain_memory(f"{verb} the studio closes at 6pm.") is True

    @pytest.mark.parametrize("verb", ["remember", "forget", "note that"])
    def test_a_memory_verb_only_counts_at_the_start_of_a_sentence(self, verb: str) -> None:
        """POL-05b / POL-06 — a mention is not an instruction.

        Unanchored, "what do you remember about my goals?" matched and every
        recall question was treated as a memory write.
        """

        assert turn_may_contain_memory(f"What do you {verb} about the studio?") is False

    def test_a_please_prefix_is_still_an_instruction(self) -> None:
        """POL-05c"""

        assert turn_may_contain_memory("Please remember the studio closes at 6pm.") is True

    @pytest.mark.parametrize(
        "phrase",
        ["changed my mind", "no longer", "not anymore", "instead of"],
    )
    def test_each_correction_phrase_opens_the_gate(self, phrase: str) -> None:
        """POL-07 — a correction states something even without first person."""

        assert turn_may_contain_memory(f"The studio hours {phrase} apply.") is True

    def test_the_word_me_alone_does_not_open_the_gate(self) -> None:
        """POL-08 — deliberate exclusion.

        "me" appears in requests for output far more often than in statements
        about the user, and a request states nothing to store.
        """

        assert turn_may_contain_memory("Give me a practice session.") is False

    @pytest.mark.parametrize(
        "opener",
        [
            "What",
            "Who",
            "When",
            "Where",
            "Why",
            "Which",
            "How",
            "Do",
            "Does",
            "Did",
            "Is",
            "Are",
            "Was",
            "Were",
            "Can",
            "Could",
            "Would",
            "Will",
            "Should",
            "Have",
            "Has",
            "Tell me",
            "Remind me",
            "List",
            "Show",
        ],
    )
    def test_every_question_opener_is_treated_as_a_question(self, opener: str) -> None:
        """POL-09 — a question is the one shape that reliably stores nothing."""

        assert turn_may_contain_memory(f"{opener} the studio hours are useful") is False


class TestProhibitedContent:
    @pytest.mark.parametrize(
        "text",
        [
            "my password is hunter2",
            "password: hunter2",
            "password = hunter2",
            "The OTP is 483920",
            "my one-time password 12345",
            "verification code: 9182",
            "my api key is abcd1234efgh",
            "api_key = abcd1234efgh",
            "access token: abcd1234efgh",
            "client secret = abcd1234efgh",
            "authentication secret is abcd1234efgh",
            "sk-abcdefghijklmnop123456",
            "-----BEGIN RSA PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----",
        ],
    )
    def test_each_prohibited_pattern_is_caught(self, text: str) -> None:
        """POL-10 — content we refuse to hold under any circumstance."""

        assert classify_sensitivity(text) is Sensitivity.PROHIBITED

    def test_a_luhn_valid_card_number_is_prohibited(self) -> None:
        """POL-11"""

        assert classify_sensitivity("my card is 4539578763621486") is Sensitivity.PROHIBITED

    def test_a_luhn_invalid_digit_run_is_not_prohibited(self) -> None:
        """POL-12 — an order number is not a card number."""

        assert classify_sensitivity("order 4539578763621487") is not Sensitivity.PROHIBITED

    def test_a_repeated_digit_run_is_not_a_card(self) -> None:
        """POL-13 — placeholder digits are not a leak."""

        assert classify_sensitivity("1111111111111111") is not Sensitivity.PROHIBITED

    @pytest.mark.parametrize(
        "text",
        ["4539 5787 6362 1486", "4539-5787-6362-1486"],
    )
    def test_card_detection_handles_separators(self, text: str) -> None:
        """POL-14a"""

        assert classify_sensitivity(f"my card is {text}") is Sensitivity.PROHIBITED

    @pytest.mark.parametrize("digits", ["123456789012", "12345678901234567890"])
    def test_digit_runs_outside_the_card_length_range_are_ignored(self, digits: str) -> None:
        """POL-14b — 13 to 19 digits only."""

        assert classify_sensitivity(f"reference {digits}") is not Sensitivity.PROHIBITED


class TestSensitiveContent:
    @pytest.mark.parametrize(
        "text",
        [
            "my diagnosis is complicated",
            "I was diagnosed with asthma",
            "my medical condition flared up",
            "my medication changed",
            "I take medication daily",
            "my mental health has been rough",
            "I have asthma",
            "I have adhd",
            "I have type 2 diabetes",
            "I have severe seasonal allergies",
            "I have arthritis",
        ],
    )
    def test_health_disclosures_are_sensitive(self, text: str) -> None:
        """POL-15a / POL-17"""

        assert classify_sensitivity(text) is Sensitivity.SENSITIVE

    @pytest.mark.parametrize(
        "text",
        [
            "my aadhaar number is 1234 5678 9012",
            "my social security is 123-45-6789",
            "my passport number is X1234567",
            "my driver's licence is D1234567",
        ],
    )
    def test_national_identifiers_are_sensitive(self, text: str) -> None:
        """POL-15b"""

        assert classify_sensitivity(text) is Sensitivity.SENSITIVE

    @pytest.mark.parametrize(
        "text",
        [
            "my bank account is 12345678",
            "my routing number is 021000021",
            "my iban is GB29 NWBK 6016 1331 9268 19",
        ],
    )
    def test_financial_identifiers_are_sensitive(self, text: str) -> None:
        """POL-15c"""

        assert classify_sensitivity(text) is Sensitivity.SENSITIVE

    @pytest.mark.parametrize(
        "text",
        [
            "my address is 221 Baker Street",
            "my home address is 42 Oak Road",
            "I live at 10 Downing Street",
            "my private address is 7 Elm Avenue",
            "my address is 15 Maple Drive",
            "my address is 3 Sunset Boulevard",
        ],
    )
    def test_street_addresses_are_sensitive(self, text: str) -> None:
        """POL-15d"""

        assert classify_sensitivity(text) is Sensitivity.SENSITIVE

    @pytest.mark.parametrize(
        "suffix",
        ["Way", "Court", "Place", "Terrace", "Crescent", "Close", "Square", "Parkway"],
    )
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Known gap: the address pattern's street-suffix list covers "
            "street/road/avenue/lane/drive/boulevard but not Way, Court, Place, "
            "Terrace, Crescent, Close, Square or Parkway. Remove this xfail when "
            "the suffix list is widened."
        ),
    )
    def test_common_street_suffixes_are_currently_missed(self, suffix: str) -> None:
        """POL-15e — a gap found while writing POL-15d, recorded not patched.

        ``_SENSITIVE_PATTERNS`` matches a house number followed by one of a
        fixed list of street suffixes.  A home address on a Way, Court, Place or
        Crescent therefore classifies as NORMAL, which means it can be stored
        without the explicit request that a home address is supposed to need.
        That is a privacy gap rather than a cosmetic one, so it is marked strict:
        widening the list turns this red and it gets promoted into POL-15d.
        """

        assert classify_sensitivity(f"my home address is 42 Oak {suffix}") is (
            Sensitivity.SENSITIVE
        )

    @pytest.mark.parametrize(
        "text",
        [
            "I have two cats",
            "I have a meeting tomorrow",
            "I have been sketching a lot",
            "I have three siblings",
            "I have a new laptop",
        ],
    )
    def test_an_ordinary_i_have_sentence_stays_normal(self, text: str) -> None:
        """POL-16 — the regression the health pattern was narrowed for.

        Left bare, the "i have" branch matched every ordinary sentence,
        classifying routine facts as sensitive and silently refusing to store
        them without an explicit request.
        """

        assert classify_sensitivity(text) is Sensitivity.NORMAL

    @pytest.mark.parametrize(
        "text",
        [
            "I want to improve at urban sketching",
            "I prefer concise answers",
            "My name is Soham",
            "I work as a designer",
        ],
    )
    def test_ordinary_facts_are_normal(self, text: str) -> None:
        """POL-18"""

        assert classify_sensitivity(text) is Sensitivity.NORMAL

    @pytest.mark.parametrize(
        "text",
        ["MY PASSWORD IS hunter2", "My Password Is hunter2"],
    )
    def test_classification_is_case_insensitive(self, text: str) -> None:
        """POL-19"""

        assert classify_sensitivity(text) is Sensitivity.PROHIBITED


class TestCandidatePersistenceDecision:
    def test_detected_sensitivity_escalates_a_declared_normal(self) -> None:
        """POL-20a — the model's own label is a floor, never a ceiling."""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value="my password is hunter2",
                display_text="my password is hunter2",
                sensitivity=Sensitivity.NORMAL,
            )
        )
        assert decision.sensitivity is Sensitivity.PROHIBITED
        assert decision.allowed is False

    def test_a_declared_sensitive_is_not_de_escalated_by_a_clean_scan(self) -> None:
        """POL-20b — escalation runs one way only."""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value="something ordinary",
                display_text="something ordinary",
                sensitivity=Sensitivity.SENSITIVE,
                explicit_user_request=True,
            )
        )
        assert decision.sensitivity is Sensitivity.SENSITIVE
        assert decision.allowed is True

    def test_prohibited_content_is_refused(self) -> None:
        """POL-21"""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value="my api key is abcd1234efgh",
                display_text="my api key is abcd1234efgh",
            )
        )
        assert decision.allowed is False
        assert decision.rejection_code is MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT

    def test_sensitive_content_without_an_explicit_request_is_refused(self) -> None:
        """POL-22 — we never quietly keep a health or financial detail."""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value="I have asthma",
                display_text="I have asthma",
                sensitivity=Sensitivity.NORMAL,
            )
        )
        assert decision.allowed is False
        assert decision.rejection_code is MemoryRejectionCode.SENSITIVE_REQUIRES_EXPLICIT_REQUEST

    def test_sensitive_content_with_an_explicit_request_is_allowed(self) -> None:
        """POL-23 — the user may always choose to have it remembered."""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value="I have asthma",
                display_text="I have asthma",
                sensitivity=Sensitivity.SENSITIVE,
                explicit_user_request=True,
            )
        )
        assert decision.allowed is True
        assert decision.rejection_code is None

    def test_the_canonical_value_is_scanned_not_just_the_display_text(self) -> None:
        """POL-24 — hiding a secret in the structured value must not work."""

        decision = candidate_persistence_decision(
            factories.proposal(
                canonical_value={"note": "my password is hunter2"},
                display_text="a harmless looking note",
            )
        )
        assert decision.allowed is False
        assert decision.rejection_code is MemoryRejectionCode.PROHIBITED_SENSITIVE_CONTENT


class TestRecallSensitivity:
    @pytest.mark.parametrize("directly_relevant", [True, False])
    def test_prohibited_content_is_never_recallable(self, directly_relevant: bool) -> None:
        """POL-25a"""

        assert (
            can_recall_sensitivity(Sensitivity.PROHIBITED, directly_relevant=directly_relevant)
            is False
        )

    def test_sensitive_content_needs_direct_relevance(self) -> None:
        """POL-25b — it surfaces when asked for, not as ambient context."""

        assert can_recall_sensitivity(Sensitivity.SENSITIVE, directly_relevant=True) is True
        assert can_recall_sensitivity(Sensitivity.SENSITIVE, directly_relevant=False) is False

    @pytest.mark.parametrize("directly_relevant", [True, False])
    def test_normal_content_is_always_recallable(self, directly_relevant: bool) -> None:
        """POL-25c"""

        assert (
            can_recall_sensitivity(Sensitivity.NORMAL, directly_relevant=directly_relevant) is True
        )


class TestDeletionPolicy:
    def test_forget_keeps_provenance_and_blocks_resurrection(self) -> None:
        """POL-26 — "forget" is reversible by the user, not by the extractor."""

        policy = deletion_policy(MemoryOperationKind.FORGET)
        assert policy.remove_recallable_content is True
        assert policy.remove_derived_indexes is True
        assert policy.remove_provenance is False
        assert policy.remove_eligible_audit_content is False
        assert policy.retain_fingerprint_tombstone_days == FORGET_TOMBSTONE_DAYS
        assert policy.automatic_resurrection_blocked is True
        assert policy.explicit_reconfirmation_allowed is True

    def test_erase_removes_everything_and_keeps_no_tombstone(self) -> None:
        """POL-27 — a true erasure leaves nothing to match against later."""

        policy = deletion_policy(MemoryOperationKind.ERASE_PERMANENTLY)
        assert policy.remove_provenance is True
        assert policy.remove_eligible_audit_content is True
        assert policy.retain_fingerprint_tombstone_days == 0
        assert policy.automatic_resurrection_blocked is False

    @pytest.mark.parametrize(
        "operation",
        [
            MemoryOperationKind.CREATE,
            MemoryOperationKind.UPDATE,
            MemoryOperationKind.ARCHIVE,
            MemoryOperationKind.RESTORE,
        ],
    )
    def test_a_non_deletion_operation_is_rejected(self, operation: MemoryOperationKind) -> None:
        """POL-28"""

        with pytest.raises(ValueError, match="deletion_policy_requires"):
            deletion_policy(operation)


class TestConflictPolicy:
    @pytest.mark.parametrize(
        "evidence",
        [
            ConflictEvidence.EXPLICIT_CORRECTION,
            ConflictEvidence.LINKED_RETRACTION_REPLACEMENT,
            ConflictEvidence.GROUNDED_SAME_SLOT_ASSERTION,
        ],
    )
    def test_clear_evidence_with_a_certain_target_replaces(
        self, evidence: ConflictEvidence
    ) -> None:
        """POL-29"""

        decision = conflict_policy_decision(evidence, target_relationship_deterministic=True)
        assert decision.action is ConflictAction.APPLY_REPLACEMENT
        assert decision.outcome is MemoryOutcome.REPLACED
        assert decision.rejection_code is None
        assert decision.existing_record_stays_active is False

    @pytest.mark.parametrize(
        "evidence",
        [
            ConflictEvidence.EXPLICIT_CORRECTION,
            ConflictEvidence.LINKED_RETRACTION_REPLACEMENT,
            ConflictEvidence.GROUNDED_SAME_SLOT_ASSERTION,
        ],
    )
    def test_clear_evidence_with_an_uncertain_target_needs_review(
        self, evidence: ConflictEvidence
    ) -> None:
        """POL-30 — knowing something changed is not knowing what to change."""

        decision = conflict_policy_decision(evidence, target_relationship_deterministic=False)
        assert decision.action is ConflictAction.NEEDS_REVIEW
        assert decision.existing_record_stays_active is True

    @pytest.mark.parametrize("deterministic", [True, False])
    def test_an_unmarked_incompatible_assertion_always_needs_review(
        self, deterministic: bool
    ) -> None:
        """POL-31 — two facts that disagree is not permission to delete one."""

        decision = conflict_policy_decision(
            ConflictEvidence.UNMARKED_INCOMPATIBLE_ASSERTION,
            target_relationship_deterministic=deterministic,
        )
        assert decision.action is ConflictAction.NEEDS_REVIEW
        assert decision.rejection_code is MemoryRejectionCode.AMBIGUOUS_CONFLICT
        assert decision.existing_record_stays_active is True


class TestExtractionTiming:
    def test_an_explicit_command_runs_before_the_response(self) -> None:
        """POL-32a — the user should see the effect in the same turn."""

        decision = extraction_timing_policy(ExtractionTrigger.EXPLICIT_MEMORY_COMMAND)
        assert decision.timing is ExtractionTiming.BEFORE_RESPONSE
        assert decision.use_current_turn_overlay is False

    def test_a_deterministic_correction_also_overlays_the_current_turn(self) -> None:
        """POL-32b — the reply must not quote the fact the user just corrected."""

        decision = extraction_timing_policy(ExtractionTrigger.DETERMINISTIC_CORRECTION)
        assert decision.timing is ExtractionTiming.BEFORE_RESPONSE
        assert decision.use_current_turn_overlay is True

    def test_automatic_extraction_waits_until_after_the_turn(self) -> None:
        """POL-33 — a model call must never sit in front of the reply."""

        decision = extraction_timing_policy(ExtractionTrigger.AUTOMATIC_LLM)
        assert decision.timing is ExtractionTiming.AFTER_TURN
        assert decision.use_current_turn_overlay is False


class TestGates:
    def test_guest_store_kind_maps_both_ways(self) -> None:
        """POL-34"""

        assert guest_store_kind(is_guest=True) is GuestStoreKind.EPHEMERAL_PROFILE
        assert guest_store_kind(is_guest=False) is GuestStoreKind.REGISTERED_PROFILE

    def test_incognito_disables_a_command(self) -> None:
        """POL-35"""

        result = gate_memory_command(
            factories.create_command(), MemoryExecutionContext(incognito=True)
        )
        assert result is not None
        assert result.outcome is MemoryOutcome.DISABLED
        assert result.rejection_code is MemoryRejectionCode.INCOGNITO_DISABLED

    def test_disabled_memory_disables_a_command(self) -> None:
        """POL-36"""

        result = gate_memory_command(
            factories.create_command(), MemoryExecutionContext(memory_enabled=False)
        )
        assert result is not None
        assert result.rejection_code is MemoryRejectionCode.MEMORY_DISABLED

    def test_incognito_takes_precedence_when_both_apply(self) -> None:
        """POL-37 — the more specific reason is the more useful one to report."""

        result = gate_memory_command(
            factories.create_command(),
            MemoryExecutionContext(memory_enabled=False, incognito=True),
        )
        assert result is not None
        assert result.rejection_code is MemoryRejectionCode.INCOGNITO_DISABLED

    def test_a_normal_context_does_not_gate(self) -> None:
        """POL-38"""

        assert gate_memory_command(factories.create_command(), MemoryExecutionContext()) is None

    @pytest.mark.parametrize(
        "rejection_code",
        [
            MemoryRejectionCode.AMBIGUOUS_CONFLICT,
            MemoryRejectionCode.USER_REJECTED,
            MemoryRejectionCode.TOO_MANY_CANDIDATES,
        ],
    )
    def test_a_disabled_result_refuses_a_non_disabled_code(
        self, rejection_code: MemoryRejectionCode
    ) -> None:
        """POL-39 — "disabled" must mean disabled, not any refusal."""

        from app.services.memory.contracts import MemoryCommandResult

        with pytest.raises(ValueError, match="disabled_result_requires_disabled_rejection_code"):
            MemoryCommandResult.disabled_for(
                factories.create_command(),
                rejection_code=rejection_code,
                message="nope",
            )


class TestPinPolicy:
    @pytest.mark.parametrize(
        "flag",
        [name for name in vars(PIN_POLICY) if name.startswith("bypasses_")],
    )
    def test_pinning_bypasses_nothing(self, flag: str) -> None:
        """POL-40 — a pin is a ranking nudge, never an access-control hole.

        If any of these ever became True, a pinned memory could outrank its
        owner check, its expiry, or its sensitivity gate.  The recall tests
        check the behaviour; this checks the declaration.
        """

        assert getattr(PIN_POLICY, flag) is False

    def test_a_pin_is_a_bounded_ranking_boost(self) -> None:
        """POL-40b"""

        assert PIN_POLICY.ranking_boost_only is True
        assert PIN_POLICY.guarantees_inclusion is False
        assert 0 < PIN_POLICY.max_score_boost <= 0.1
