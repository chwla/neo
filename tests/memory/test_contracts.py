"""Tier 1 — command and result contracts (plan section CON).

The contracts are the memory layer's front door.  Every mutation arrives as one
of nine commands and leaves as one result, and the validators here are what stop
a malformed or self-contradictory request from ever reaching the planner.  A
result that claims success while carrying a rejection code, or a replace command
with nothing to replace, should be impossible to construct — not merely unusual.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.services.memory.contracts import (
    MEMORY_COMMAND_ADAPTER,
    CandidateDecisionResult,
    CandidateGroundingSpan,
    CandidateIntent,
    CandidateLifecycleState,
    CandidateProposal,
    CandidateTargetHints,
    EvidenceRole,
    EvidenceSpan,
    MemoryActor,
    MemoryCommandResult,
    MemoryErrorCode,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MemoryUpdatePatch,
    PersistExtractionCandidateCommand,
    ReplacementAuthority,
    RestoreMode,
    Sensitivity,
    SourceChangeOutcome,
    SourceChangeResult,
    TargetRevision,
    ValidatedCandidateProposal,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory import factories
from tests.memory.conftest import OWNER_ID

VALID_HASH = "a" * 64


class TestContractModelBehaviour:
    def test_extra_fields_are_forbidden(self) -> None:
        """CON-01a — a typo in a field name must fail, not be ignored."""

        with pytest.raises(ValidationError):
            MemoryActor(kind="user", actor_id="x", extra_field="oops")

    def test_models_are_frozen(self) -> None:
        """CON-01b — nothing mutates a command after validation."""

        actor = factories.actor()
        with pytest.raises(ValidationError):
            actor.actor_id = "someone-else"

    def test_whitespace_is_stripped(self) -> None:
        """CON-01c"""

        assert MemoryActor(kind="user", actor_id="  spaced  ").actor_id == "spaced"

    @pytest.mark.parametrize(
        ("supplied", "expected"),
        [
            ("11111111-1111-4111-8111-111111111111", OWNER_ID),
            ("11111111-1111-4111-8111-111111111111".upper(), OWNER_ID),
            ("{11111111-1111-4111-8111-111111111111}", OWNER_ID),
            ("  11111111-1111-4111-8111-111111111111  ", OWNER_ID),
        ],
    )
    def test_an_owner_id_is_canonicalised(self, supplied: str, expected: str) -> None:
        """CON-02a"""

        assert factories.create_command(owner=supplied).owner_id == expected

    @pytest.mark.parametrize("supplied", ["not-a-uuid", "", "1234", "11111111-1111-4111-8111"])
    def test_a_malformed_owner_id_is_rejected(self, supplied: str) -> None:
        """CON-02b"""

        with pytest.raises(ValidationError):
            factories.create_command(owner=supplied)


class TestEvidenceSpan:
    def test_start_and_end_must_be_supplied_together(self) -> None:
        """CON-03"""

        with pytest.raises(ValidationError, match="span_start_and_end_must_be_supplied_together"):
            EvidenceSpan(role=EvidenceRole.ASSERTION, text="x", start=0)
        with pytest.raises(ValidationError, match="span_start_and_end_must_be_supplied_together"):
            EvidenceSpan(role=EvidenceRole.ASSERTION, text="x", end=5)

    @pytest.mark.parametrize(("start", "end"), [(5, 5), (5, 4), (10, 1)])
    def test_the_end_must_follow_the_start(self, start: int, end: int) -> None:
        """CON-04"""

        with pytest.raises(ValidationError, match="span_end_must_follow_start"):
            EvidenceSpan(role=EvidenceRole.ASSERTION, text="x", start=start, end=end)

    @pytest.mark.parametrize(("start", "end"), [(-1, 5), (0, 0)])
    def test_out_of_range_offsets_are_rejected(self, start: int, end: int) -> None:
        """CON-05"""

        with pytest.raises(ValidationError):
            EvidenceSpan(role=EvidenceRole.ASSERTION, text="x", start=start, end=end)

    def test_a_span_without_offsets_is_valid(self) -> None:
        """CON-05b — offsets are optional; a quoted text alone is allowed."""

        assert EvidenceSpan(role=EvidenceRole.ASSERTION, text="x").start is None


class TestCandidateProposal:
    @pytest.mark.parametrize("value", [None, "", "   ", "\t"])
    def test_an_empty_canonical_value_is_rejected(self, value: object) -> None:
        """CON-06 — a memory has to record something."""

        with pytest.raises(ValidationError, match="positive_canonical_value_required"):
            factories.proposal(canonical_value=value)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("confidence", -0.1),
            ("confidence", 1.1),
            ("importance", 0),
            ("importance", 11),
            ("value_schema_version", 0),
        ],
    )
    def test_out_of_range_scalars_are_rejected(self, field: str, value: object) -> None:
        """CON-07"""

        with pytest.raises(ValidationError):
            factories.proposal(**{field: value})

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("confidence", 0.0),
            ("confidence", 1.0),
            ("importance", 1),
            ("importance", 10),
        ],
    )
    def test_the_extremes_of_each_range_are_accepted(self, field: str, value: object) -> None:
        """CON-07b"""

        assert getattr(factories.proposal(**{field: value}), field) == value

    def test_a_validated_proposal_refuses_prohibited_content(self) -> None:
        """CON-08 — prohibited content can never become durable."""

        with pytest.raises(ValidationError, match="prohibited_candidate_cannot_be_validated"):
            factories.proposal(sensitivity=Sensitivity.PROHIBITED)

    def test_a_validated_sensitive_proposal_needs_an_explicit_request(self) -> None:
        """CON-09"""

        with pytest.raises(
            ValidationError, match="sensitive_candidate_requires_explicit_user_request"
        ):
            factories.proposal(sensitivity=Sensitivity.SENSITIVE, explicit_user_request=False)

    def test_an_unvalidated_proposal_may_hold_prohibited_content(self) -> None:
        """CON-08b — the base model is what the classifier inspects.

        Only ``ValidatedCandidateProposal`` promises durability, so the base
        class must still be constructible for content we intend to refuse.
        """

        proposal = CandidateProposal(
            memory_type=MemoryType.GOAL,
            domain_key="global",
            slot_key=factories.DEFAULT_GOAL_SLOT,
            cardinality=Cardinality.ADDITIVE,
            canonical_value="x",
            display_text="x",
            sensitivity=Sensitivity.PROHIBITED,
            confidence=0.5,
            importance=5,
        )
        assert proposal.sensitivity is Sensitivity.PROHIBITED


class TestTargetHints:
    @pytest.mark.parametrize(
        "hints",
        [
            CandidateTargetHints(target_memory_ids=(uuid4(),)),
            CandidateTargetHints(old_value_phrases=("the old value",)),
            CandidateTargetHints(predecessor_domain_key="learning"),
            CandidateTargetHints(predecessor_slot_key="goal:learning:primary_output"),
        ],
    )
    def test_each_evidence_kind_counts_as_target_evidence(
        self, hints: CandidateTargetHints
    ) -> None:
        """CON-10a"""

        assert hints.has_target_evidence is True

    def test_empty_hints_carry_no_target_evidence(self) -> None:
        """CON-10b"""

        assert CandidateTargetHints().has_target_evidence is False

    def test_an_explicit_change_flag_alone_is_not_target_evidence(self) -> None:
        """CON-10c — saying "the domain changed" does not say which record."""

        hints = CandidateTargetHints(explicit_domain_change=True, explicit_slot_change=True)
        assert hints.has_target_evidence is False


class TestGroundingSpan:
    @pytest.mark.parametrize(
        "content_hash",
        ["", "abc", "A" * 64, "g" * 64, "a" * 63, "a" * 65],
    )
    def test_a_malformed_content_hash_is_rejected(self, content_hash: str) -> None:
        """CON-11 — the hash is what ties a span to the message it quotes."""

        with pytest.raises(ValidationError):
            CandidateGroundingSpan(
                message_id="m1",
                role=EvidenceRole.ASSERTION,
                start=0,
                end=5,
                content_hash=content_hash,
            )

    def test_a_valid_span_is_accepted(self) -> None:
        """CON-11b"""

        span = CandidateGroundingSpan(
            message_id="m1",
            role=EvidenceRole.ASSERTION,
            start=0,
            end=5,
            content_hash=VALID_HASH,
        )
        assert span.content_hash == VALID_HASH

    def test_the_end_must_follow_the_start(self) -> None:
        """CON-11c"""

        with pytest.raises(ValidationError, match="span_end_must_follow_start"):
            CandidateGroundingSpan(
                message_id="m1",
                role=EvidenceRole.ASSERTION,
                start=5,
                end=5,
                content_hash=VALID_HASH,
            )


class TestUpdatePatch:
    def test_an_empty_patch_is_rejected(self) -> None:
        """CON-12 — an update that changes nothing is a caller bug."""

        with pytest.raises(ValidationError, match="update_patch_requires_a_field"):
            MemoryUpdatePatch()

    def test_an_explicitly_null_canonical_value_is_rejected(self) -> None:
        """CON-13 — clearing a value is not an update, it is a deletion."""

        with pytest.raises(ValidationError, match="canonical_value_cannot_be_null"):
            MemoryUpdatePatch(canonical_value=None)

    def test_unset_and_explicitly_null_are_distinguished(self) -> None:
        """CON-14 — the whole patch mechanism rests on this distinction."""

        patch = MemoryUpdatePatch(importance=7)
        assert "canonical_value" not in patch.model_fields_set
        assert "importance" in patch.model_fields_set

    def test_a_single_field_patch_is_valid(self) -> None:
        """CON-12b"""

        assert MemoryUpdatePatch(pinned=True).pinned is True


class TestCommandValidators:
    def test_replace_requires_a_replace_intent_candidate(self) -> None:
        """CON-15"""

        with pytest.raises(ValidationError, match="replace_command_requires_replace_candidate"):
            factories.replace_command(
                candidate=factories.proposal(intent=CandidateIntent.ASSERT),
                targets=(TargetRevision(memory_id=uuid4(), expected_revision=1),),
            )

    def test_replace_requires_target_evidence(self) -> None:
        """CON-16a — replacing without knowing what you replace is destructive."""

        with pytest.raises(ValidationError, match="replace_command_requires_target_evidence"):
            factories.replace_command(
                candidate=factories.proposal(intent=CandidateIntent.REPLACE),
                authority=ReplacementAuthority.EXPLICIT_CORRECTION,
                targets=(),
            )

    def test_a_grounded_same_slot_assertion_may_omit_targets(self) -> None:
        """CON-16b — the slot itself identifies the record to replace."""

        command = factories.replace_command(
            candidate=factories.proposal(intent=CandidateIntent.REPLACE),
            authority=ReplacementAuthority.GROUNDED_SAME_SLOT_ASSERTION,
            targets=(),
        )
        assert command.authority is ReplacementAuthority.GROUNDED_SAME_SLOT_ASSERTION

    def test_target_hints_satisfy_the_evidence_requirement(self) -> None:
        """CON-16c"""

        command = factories.replace_command(
            candidate=factories.proposal(
                intent=CandidateIntent.REPLACE,
                target_hints=CandidateTargetHints(old_value_phrases=("the old goal",)),
            ),
            targets=(),
        )
        assert command.candidate.target_hints.has_target_evidence

    def test_supersede_requires_at_least_one_predecessor(self) -> None:
        """CON-17"""

        with pytest.raises(ValidationError):
            factories.supersede_command(predecessors=(), successor_memory_id=uuid4())

    def test_merge_requires_at_least_two_sources(self) -> None:
        """CON-18 — merging one record into itself is not a merge."""

        with pytest.raises(ValidationError):
            factories.merge_command(
                sources=(TargetRevision(memory_id=uuid4(), expected_revision=1),)
            )

    def test_restore_as_replacement_requires_a_candidate(self) -> None:
        """CON-19"""

        with pytest.raises(ValidationError, match="restore_as_replacement_requires_candidate"):
            factories.restore_command(memory_id=uuid4(), mode=RestoreMode.AS_REPLACEMENT)

    def test_an_archived_restore_forbids_a_replacement_candidate(self) -> None:
        """CON-20 — restoring in place and replacing are different operations."""

        with pytest.raises(
            ValidationError, match="archived_restore_cannot_include_replacement_candidate"
        ):
            factories.restore_command(
                memory_id=uuid4(),
                mode=RestoreMode.ARCHIVED_ONLY,
                replacement_candidate=factories.proposal(),
            )


class TestCommandDiscrimination:
    @staticmethod
    def _build(operation: MemoryOperationKind, memory_id):
        builders = {
            MemoryOperationKind.CREATE: lambda: factories.create_command(),
            MemoryOperationKind.UPDATE: lambda: factories.update_command(memory_id=memory_id),
            MemoryOperationKind.REPLACE: lambda: factories.replace_command(
                candidate=factories.proposal(intent=CandidateIntent.REPLACE),
                targets=(TargetRevision(memory_id=memory_id, expected_revision=1),),
            ),
            MemoryOperationKind.SUPERSEDE: lambda: factories.supersede_command(
                predecessors=(TargetRevision(memory_id=memory_id, expected_revision=1),),
                successor_memory_id=uuid4(),
            ),
            MemoryOperationKind.MERGE: lambda: factories.merge_command(
                sources=(
                    TargetRevision(memory_id=memory_id, expected_revision=1),
                    TargetRevision(memory_id=uuid4(), expected_revision=1),
                ),
            ),
            MemoryOperationKind.ARCHIVE: lambda: factories.archive_command(memory_id=memory_id),
            MemoryOperationKind.FORGET: lambda: factories.forget_command(memory_id=memory_id),
            MemoryOperationKind.ERASE_PERMANENTLY: lambda: factories.erase_command(
                memory_id=memory_id
            ),
            MemoryOperationKind.RESTORE: lambda: factories.restore_command(memory_id=memory_id),
        }
        return builders[operation]()

    @pytest.mark.parametrize("operation", list(MemoryOperationKind))
    def test_every_operation_kind_round_trips_through_the_adapter(
        self, operation: MemoryOperationKind
    ) -> None:
        """CON-21 / CON-35 — the wire format is the contract.

        Every kind, with no exclusions: ``update`` used to be excluded because it
        could not survive a JSON round trip (CON-21b), which is now fixed.
        """

        memory_id = uuid4()
        command = self._build(operation, memory_id)
        reparsed = MEMORY_COMMAND_ADAPTER.validate_python(command.model_dump(mode="json"))
        assert reparsed.operation is operation
        assert reparsed == command

    def test_an_update_command_round_trips_through_the_adapter(self) -> None:
        """CON-21b — fixed. The audit record of an update is replayable again.

        ``MemoryUpdatePatch`` treats a key being *present* as intent to change
        that field, and separately forbids ``canonical_value`` from being null.
        A full JSON dump wrote every optional field as null, so re-parsing saw an
        explicit ``canonical_value: None`` and refused.

        Reachable rather than theoretical: ``mutations.py`` stores exactly this
        dump in ``memory_operations.normalized_command_json`` and ``execute()``
        re-parses dicts through this adapter, so the stored audit record of an
        update could not be replayed through the door that wrote it.

        The patch now serialises only the fields that were set, so the wire
        format carries the same unset-versus-null distinction the validator
        relies on.
        """

        command = factories.update_command(memory_id=uuid4())
        reparsed = MEMORY_COMMAND_ADAPTER.validate_python(command.model_dump(mode="json"))
        assert reparsed == command

    def test_an_update_dump_carries_only_what_the_caller_set(self) -> None:
        """CON-21c — the audit row's shape, which is the fix seen from outside.

        Previously every optional patch field appeared as null. Now only the
        fields the caller actually set are present, which is what makes the dump
        re-parseable and also makes the audit row say what was changed rather
        than listing everything that was not.
        """

        dumped = factories.update_command(memory_id=uuid4()).model_dump(mode="json")
        assert dumped["patch"] == {"importance": 7}
        assert dumped["operation"] == "update"

    def test_a_patch_field_set_to_null_is_still_transmitted(self) -> None:
        """CON-21d — "clear this" and "leave this alone" stay different messages.

        Filtering the dump to fields that were set would be wrong if it also
        dropped an explicit null, since `expires_at=None` means "remove the
        expiry" while omitting it means "do not touch it". An explicitly set
        field is in `model_fields_set`, so it survives.
        """

        from app.services.memory.contracts import MemoryUpdatePatch

        patch = MemoryUpdatePatch(importance=7, expires_at=None)
        assert patch.model_dump(mode="json") == {"importance": 7, "expires_at": None}
        assert MemoryUpdatePatch(importance=7).model_dump(mode="json") == {"importance": 7}

    def test_an_unknown_operation_is_rejected(self) -> None:
        """CON-22"""

        payload = factories.create_command().model_dump(mode="json")
        payload["operation"] = "teleport"
        with pytest.raises(ValidationError):
            MEMORY_COMMAND_ADAPTER.validate_python(payload)

    def test_a_missing_operation_is_rejected(self) -> None:
        """CON-22b"""

        payload = factories.create_command().model_dump(mode="json")
        del payload["operation"]
        with pytest.raises(ValidationError):
            MEMORY_COMMAND_ADAPTER.validate_python(payload)


class TestCommandResult:
    @pytest.mark.parametrize(
        "outcome",
        [MemoryOutcome.NEEDS_REVIEW, MemoryOutcome.REJECTED, MemoryOutcome.DISABLED],
    )
    def test_a_refusal_must_say_why(self, outcome: MemoryOutcome) -> None:
        """CON-23"""

        with pytest.raises(ValidationError, match="rejection_outcome_requires_rejection_code"):
            MemoryCommandResult(
                owner_id=OWNER_ID, operation=MemoryOperationKind.CREATE, outcome=outcome
            )

    @pytest.mark.parametrize(
        "outcome",
        [MemoryOutcome.CREATED, MemoryOutcome.REFINED, MemoryOutcome.FORGOTTEN],
    )
    def test_a_success_cannot_carry_a_rejection_code(self, outcome: MemoryOutcome) -> None:
        """CON-24 — a result that both succeeded and was refused is a bug."""

        with pytest.raises(ValidationError, match="cannot_have_rejection_code"):
            MemoryCommandResult(
                owner_id=OWNER_ID,
                operation=MemoryOperationKind.CREATE,
                outcome=outcome,
                rejection_code=MemoryRejectionCode.AMBIGUOUS_CONFLICT,
            )

    def test_a_failure_must_carry_an_error_code(self) -> None:
        """CON-25a"""

        with pytest.raises(ValidationError, match="failed_outcome_requires_error_code"):
            MemoryCommandResult(
                owner_id=OWNER_ID,
                operation=MemoryOperationKind.CREATE,
                outcome=MemoryOutcome.FAILED,
            )

    def test_a_non_failure_cannot_carry_an_error_code(self) -> None:
        """CON-25b"""

        with pytest.raises(ValidationError, match="non_failed_outcome_cannot_have_error_code"):
            MemoryCommandResult(
                owner_id=OWNER_ID,
                operation=MemoryOperationKind.CREATE,
                outcome=MemoryOutcome.CREATED,
                error_code=MemoryErrorCode.INTERNAL_ERROR,
            )


class TestSourceChangeResult:
    @staticmethod
    def _result(**overrides):
        base = {
            "outcome": SourceChangeOutcome.PRESERVED,
            "owner_id": OWNER_ID,
            "memory_id": uuid4(),
            "requested_source_id": uuid4(),
            "detached_source_id": uuid4(),
            "remaining_active_source_count": 2,
            "review_required": False,
            "idempotency_key": "k",
            "reason": "detached",
        }
        base.update(overrides)
        return SourceChangeResult(**base)

    def test_a_preserved_memory_must_still_have_support(self) -> None:
        """CON-26a — "preserved" means something else still vouches for it."""

        with pytest.raises(ValidationError, match="preserved_source_change_requires_remaining"):
            self._result(remaining_active_source_count=0)

    def test_a_preserved_memory_cannot_also_need_review(self) -> None:
        """CON-26b"""

        with pytest.raises(ValidationError, match="preserved_source_change_cannot_require"):
            self._result(review_required=True)

    def test_losing_the_last_source_must_require_review(self) -> None:
        """CON-27 — an unsupported memory is a decision for the user."""

        with pytest.raises(ValidationError, match="final_source_change_requires_review"):
            self._result(
                outcome=SourceChangeOutcome.NEEDS_REVIEW,
                remaining_active_source_count=1,
                review_required=True,
            )

    def test_a_valid_needs_review_result_is_accepted(self) -> None:
        """CON-27b"""

        result = self._result(
            outcome=SourceChangeOutcome.NEEDS_REVIEW,
            remaining_active_source_count=0,
            review_required=True,
        )
        assert result.review_required is True

    @pytest.mark.parametrize(
        "outcome",
        [
            SourceChangeOutcome.SOURCE_NOT_FOUND,
            SourceChangeOutcome.OWNER_MISMATCH,
            SourceChangeOutcome.REVISION_CONFLICT,
        ],
    )
    def test_a_non_detaching_outcome_cannot_claim_a_detached_source(
        self, outcome: SourceChangeOutcome
    ) -> None:
        """CON-28"""

        with pytest.raises(ValidationError, match="non_detached_outcome_cannot_claim"):
            self._result(outcome=outcome, review_required=False)

    @pytest.mark.parametrize(
        "outcome",
        [
            SourceChangeOutcome.PRESERVED,
            SourceChangeOutcome.NEEDS_REVIEW,
            SourceChangeOutcome.ALREADY_DETACHED,
        ],
    )
    def test_a_detaching_outcome_must_name_the_source(self, outcome: SourceChangeOutcome) -> None:
        """CON-28b"""

        with pytest.raises(ValidationError, match="detached_outcome_requires_source_id"):
            self._result(
                outcome=outcome,
                detached_source_id=None,
                remaining_active_source_count=0
                if outcome is not (SourceChangeOutcome.PRESERVED)
                else 2,
                review_required=outcome is SourceChangeOutcome.NEEDS_REVIEW,
            )

    @pytest.mark.parametrize(
        "outcome",
        [
            SourceChangeOutcome.SOURCE_NOT_FOUND,
            SourceChangeOutcome.OWNER_MISMATCH,
            SourceChangeOutcome.REVISION_CONFLICT,
        ],
    )
    def test_a_failed_lookup_cannot_require_review(self, outcome: SourceChangeOutcome) -> None:
        """CON-29 — nothing changed, so there is nothing to review."""

        with pytest.raises(ValidationError, match="non_applied_source_change_cannot_require"):
            self._result(outcome=outcome, detached_source_id=None, review_required=True)

    def test_a_source_change_never_claims_a_canonical_mutation(self) -> None:
        """CON-26c — detaching provenance must not touch the fact itself."""

        result = self._result()
        assert result.canonical_mutation_performed is False
        assert result.canonical_revision_changed is False


class TestPersistExtractionCandidate:
    @staticmethod
    def _command(**overrides) -> PersistExtractionCandidateCommand:
        base = {
            "owner_id": OWNER_ID,
            "candidate": factories.proposal(),
            "state": CandidateLifecycleState.VALIDATED,
            "decision_reason": "grounded",
            "source_message_id": "m1",
            "source_spans": (
                CandidateGroundingSpan(
                    message_id="m1",
                    role=EvidenceRole.ASSERTION,
                    start=0,
                    end=5,
                    content_hash=VALID_HASH,
                ),
            ),
            "extractor_name": "test",
            "extractor_version": "v1",
        }
        base.update(overrides)
        return PersistExtractionCandidateCommand(**base)

    def test_a_review_candidate_must_carry_its_outcome_and_code(self) -> None:
        """CON-30"""

        with pytest.raises(ValidationError, match="review_candidate_requires_outcome_and_code"):
            self._command(state=CandidateLifecycleState.NEEDS_REVIEW)

    def test_a_validated_candidate_cannot_carry_a_decision(self) -> None:
        """CON-31 — it has not been decided yet; that is what validated means."""

        with pytest.raises(ValidationError, match="validated_candidate_cannot_have_decision"):
            self._command(decision_outcome=MemoryOutcome.NEEDS_REVIEW)

    def test_at_least_one_source_span_is_required(self) -> None:
        """CON-32 — an ungrounded candidate is not persistable."""

        with pytest.raises(ValidationError):
            self._command(source_spans=())

    def test_a_valid_review_candidate_is_accepted(self) -> None:
        """CON-30b"""

        command = self._command(
            state=CandidateLifecycleState.NEEDS_REVIEW,
            decision_outcome=MemoryOutcome.NEEDS_REVIEW,
            rejection_code=MemoryRejectionCode.AMBIGUOUS_CONFLICT,
        )
        assert command.state is CandidateLifecycleState.NEEDS_REVIEW

    @pytest.mark.parametrize("raw_hash", ["abc", "A" * 64, "z" * 64])
    def test_a_malformed_raw_output_hash_is_rejected(self, raw_hash: str) -> None:
        """CON-32b"""

        with pytest.raises(ValidationError):
            self._command(raw_output_hash=raw_hash)


class TestCandidateDecisionResult:
    @pytest.mark.parametrize(
        "state",
        [CandidateLifecycleState.NEEDS_REVIEW, CandidateLifecycleState.REJECTED],
    )
    def test_a_refusing_state_must_carry_a_code(self, state: CandidateLifecycleState) -> None:
        """CON-33"""

        with pytest.raises(ValidationError, match="candidate_rejection_state_requires_code"):
            CandidateDecisionResult(
                candidate_id=uuid4(), state=state, outcome=MemoryOutcome.NEEDS_REVIEW
            )

    def test_an_applied_candidate_must_name_its_operation(self) -> None:
        """CON-34 — provenance: every applied candidate traces to one write."""

        with pytest.raises(ValidationError, match="applied_candidate_requires_operation_id"):
            CandidateDecisionResult(
                candidate_id=uuid4(),
                state=CandidateLifecycleState.APPLIED,
                outcome=MemoryOutcome.CREATED,
            )

    def test_a_valid_applied_result_is_accepted(self) -> None:
        """CON-34b"""

        result = CandidateDecisionResult(
            candidate_id=uuid4(),
            state=CandidateLifecycleState.APPLIED,
            outcome=MemoryOutcome.CREATED,
            operation_id=uuid4(),
        )
        assert result.operation_id is not None


class TestValidatedProposalRoundTrip:
    def test_a_validated_proposal_survives_serialisation(self) -> None:
        """CON-35b"""

        original = factories.proposal()
        reparsed = ValidatedCandidateProposal.model_validate(original.model_dump(mode="json"))
        assert reparsed == original
