from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from app.services.memory.contracts import (
    MEMORY_COMMAND_ADAPTER,
    ActorKind,
    ArchiveMemoryCommand,
    CandidateLifecycleState,
    CreateMemoryCommand,
    ErasePermanentlyMemoryCommand,
    ForgetMemoryCommand,
    MemoryActor,
    MemoryCommandResult,
    MemoryErrorCode,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MemorySource,
    MemoryUpdatePatch,
    Sensitivity,
    SourceKind,
    TargetRevision,
    UpdateMemoryCommand,
    ValidatedCandidateProposal,
)
from app.services.memory.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION

MEMORY_ID = UUID("00000000-0000-4000-8000-000000000201")
OWNER_ID = "00000000-0000-4000-8000-000000000001"


def test_create_command_json_round_trip_is_discriminated_and_versioned(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    command = CreateMemoryCommand(
        owner_id=OWNER_ID,
        idempotency_key="chat:message-1:goal-1",
        actor=user_actor,
        source=direct_source,
        candidate=normal_goal_candidate,
    )

    parsed = MEMORY_COMMAND_ADAPTER.validate_json(command.model_dump_json())

    assert parsed == command
    payload = command.model_dump(mode="json")
    assert payload["contract_version"] == CONTRACT_VERSION
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["taxonomy_version"] == TAXONOMY_VERSION
    assert payload["operation"] == "create"
    assert payload["candidate"]["sensitivity"] == "normal"


def test_contracts_forbid_unknown_fields(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    with pytest.raises(ValidationError):
        CreateMemoryCommand.model_validate(
            {
                "owner_id": OWNER_ID,
                "idempotency_key": "idempotent-1",
                "actor": user_actor.model_dump(),
                "source": direct_source.model_dump(),
                "candidate": normal_goal_candidate.model_dump(),
                "unapproved_field": True,
            }
        )


def test_mutating_known_record_requires_expected_revision(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    with pytest.raises(ValidationError):
        UpdateMemoryCommand.model_validate(
            {
                "owner_id": OWNER_ID,
                "idempotency_key": "update-1",
                "actor": user_actor.model_dump(),
                "source": direct_source.model_dump(),
                "target": {"memory_id": str(MEMORY_ID)},
                "patch": {"importance": normal_goal_candidate.importance + 1},
            }
        )


def test_update_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        MemoryUpdatePatch()


def test_forget_and_permanent_erasure_are_distinct_commands(
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    common = {
        "owner_id": OWNER_ID,
        "actor": user_actor,
        "source": direct_source,
        "target": TargetRevision(memory_id=MEMORY_ID, expected_revision=3),
    }
    forget = ForgetMemoryCommand(idempotency_key="forget-1", **common)
    erase = ErasePermanentlyMemoryCommand(idempotency_key="erase-1", **common)

    assert forget.operation is MemoryOperationKind.FORGET
    assert erase.operation is MemoryOperationKind.ERASE_PERMANENTLY
    assert "delete" not in {operation.value for operation in MemoryOperationKind}


def test_stable_operation_outcome_and_code_vocabularies() -> None:
    assert {operation.value for operation in MemoryOperationKind} == {
        "create",
        "update",
        "replace",
        "supersede",
        "merge",
        "archive",
        "forget",
        "erase_permanently",
        "restore",
    }
    assert {outcome.value for outcome in MemoryOutcome} == {
        "created",
        "reconfirmed",
        "refined",
        "replaced",
        "superseded",
        "merged",
        "archived",
        "forgotten",
        "erased_permanently",
        "restored",
        "needs_review",
        "rejected",
        "disabled",
        "failed",
    }
    assert {code.value for code in MemoryRejectionCode} == {
        "ambiguous_conflict",
        "conflict_requires_replace",
        "incognito_disabled",
        "memory_disabled",
        "prohibited_sensitive_content",
        "sensitive_requires_explicit_request",
        "resurrection_blocked",
        "replacement_target_not_found",
        "positive_value_required",
        "ungrounded_candidate",
        "too_many_candidates",
        "invalid_restore",
        "user_rejected",
    }
    assert {code.value for code in MemoryErrorCode} == {
        "invalid_command",
        "owner_required",
        "owner_mismatch",
        "cross_owner_reference",
        "not_found",
        "expected_revision_required",
        "revision_conflict",
        "idempotency_conflict",
        "unsupported_contract_version",
        "unsupported_policy_version",
        "unsupported_taxonomy_version",
        "internal_error",
    }


def test_result_requires_code_matching_its_outcome() -> None:
    with pytest.raises(ValidationError):
        MemoryCommandResult(
            owner_id=OWNER_ID,
            operation=MemoryOperationKind.CREATE,
            outcome=MemoryOutcome.DISABLED,
        )
    failed = MemoryCommandResult(
        owner_id=OWNER_ID,
        operation=MemoryOperationKind.UPDATE,
        outcome=MemoryOutcome.FAILED,
        error_code=MemoryErrorCode.REVISION_CONFLICT,
    )
    assert failed.error_code is MemoryErrorCode.REVISION_CONFLICT


def test_validated_candidate_rejects_prohibited_sensitivity(
    normal_goal_candidate: ValidatedCandidateProposal,
) -> None:
    payload = normal_goal_candidate.model_dump()
    payload["sensitivity"] = Sensitivity.PROHIBITED
    with pytest.raises(ValidationError, match="prohibited_candidate_cannot_be_validated"):
        ValidatedCandidateProposal.model_validate(payload)


def test_actor_and_source_kinds_are_serializable() -> None:
    actor = MemoryActor(kind=ActorKind.MAINTENANCE, actor_id="maintenance-1")
    source = MemorySource(kind=SourceKind.MAINTENANCE, source_id="audit-1")
    assert actor.model_dump(mode="json") == {
        "kind": "maintenance",
        "actor_id": "maintenance-1",
    }
    assert source.model_dump(mode="json")["kind"] == "maintenance"


def test_archive_command_uses_revisioned_target(
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    command = ArchiveMemoryCommand(
        owner_id=OWNER_ID,
        idempotency_key="archive-1",
        actor=user_actor,
        source=direct_source,
        target=TargetRevision(memory_id=MEMORY_ID, expected_revision=4),
    )
    assert command.target.expected_revision == 4
    assert CandidateLifecycleState.VALIDATED.value == "validated"


def test_owner_id_is_normalized_and_must_be_a_uuid(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    command = CreateMemoryCommand(
        owner_id=OWNER_ID.upper(),
        idempotency_key="owner-normalization",
        actor=user_actor,
        source=direct_source,
        candidate=normal_goal_candidate,
    )
    assert command.owner_id == OWNER_ID
    with pytest.raises(ValidationError, match="canonical_uuid_required"):
        CreateMemoryCommand(
            owner_id="username-is-not-an-owner",
            idempotency_key="invalid-owner",
            actor=user_actor,
            source=direct_source,
            candidate=normal_goal_candidate,
        )
