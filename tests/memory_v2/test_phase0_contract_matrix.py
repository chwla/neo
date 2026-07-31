from __future__ import annotations

import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.services.memory_v2.contracts import (
    MEMORY_COMMAND_ADAPTER,
    ArchiveMemoryCommand,
    CandidateIntent,
    CreateMemoryCommand,
    ErasePermanentlyMemoryCommand,
    ForgetMemoryCommand,
    MemoryActor,
    MemorySource,
    MemoryUpdatePatch,
    MergeMemoryCommand,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    RestoreMemoryCommand,
    RestoreMode,
    SupersedeMemoryCommand,
    TargetRevision,
    UpdateMemoryCommand,
    ValidatedCandidateProposal,
)

OWNER_ID = "00000000-0000-4000-8000-000000000001"
MEMORY_A = UUID("00000000-0000-4000-8000-000000000201")
MEMORY_B = UUID("00000000-0000-4000-8000-000000000202")
MEMORY_C = UUID("00000000-0000-4000-8000-000000000203")


def _common(actor: MemoryActor, source: MemorySource, key: str) -> dict[str, object]:
    return {
        "owner_id": OWNER_ID,
        "idempotency_key": key,
        "actor": actor,
        "source": source,
    }


def test_every_command_has_a_stable_discriminated_json_round_trip(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    target_a = TargetRevision(memory_id=MEMORY_A, expected_revision=2)
    target_b = TargetRevision(memory_id=MEMORY_B, expected_revision=3)
    replacement = normal_goal_candidate.model_copy(update={"intent": CandidateIntent.REPLACE})
    commands = (
        CreateMemoryCommand(
            **_common(user_actor, direct_source, "create"),
            candidate=normal_goal_candidate,
        ),
        UpdateMemoryCommand(
            **_common(user_actor, direct_source, "update"),
            target=target_a,
            patch=MemoryUpdatePatch(importance=8),
        ),
        ReplaceMemoryCommand(
            **_common(user_actor, direct_source, "replace"),
            candidate=replacement,
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(target_a,),
        ),
        SupersedeMemoryCommand(
            **_common(user_actor, direct_source, "supersede"),
            predecessors=(target_a,),
            successor_memory_id=MEMORY_C,
        ),
        MergeMemoryCommand(
            **_common(user_actor, direct_source, "merge"),
            sources=(target_a, target_b),
            candidate=normal_goal_candidate,
        ),
        ArchiveMemoryCommand(
            **_common(user_actor, direct_source, "archive"),
            target=target_a,
        ),
        ForgetMemoryCommand(
            **_common(user_actor, direct_source, "forget"),
            target=target_a,
        ),
        ErasePermanentlyMemoryCommand(
            **_common(user_actor, direct_source, "erase"),
            target=target_a,
        ),
        RestoreMemoryCommand(
            **_common(user_actor, direct_source, "restore"),
            target=target_a,
        ),
    )

    for command in commands:
        payload = command.model_dump(mode="json", exclude_unset=True)
        payload["operation"] = command.operation.value
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        assert MEMORY_COMMAND_ADAPTER.validate_json(serialized) == command


def test_command_base_requires_nonempty_idempotency_key(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    for missing in (None, ""):
        payload = {
            "owner_id": OWNER_ID,
            "actor": user_actor,
            "source": direct_source,
            "candidate": normal_goal_candidate,
        }
        if missing is not None:
            payload["idempotency_key"] = missing
        with pytest.raises(ValidationError):
            CreateMemoryCommand.model_validate(payload)


def test_invalid_replace_merge_supersede_and_restore_combinations_are_rejected(
    normal_goal_candidate: ValidatedCandidateProposal,
    user_actor: MemoryActor,
    direct_source: MemorySource,
) -> None:
    common = _common(user_actor, direct_source, "invalid")
    target = TargetRevision(memory_id=MEMORY_A, expected_revision=1)

    with pytest.raises(ValidationError, match="replace_command_requires_replace_candidate"):
        ReplaceMemoryCommand(
            **common,
            candidate=normal_goal_candidate,
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(target,),
        )
    with pytest.raises(ValidationError):
        SupersedeMemoryCommand(
            **common,
            predecessors=(),
            successor_memory_id=MEMORY_B,
        )
    with pytest.raises(ValidationError):
        MergeMemoryCommand(
            **common,
            sources=(target,),
            candidate=normal_goal_candidate,
        )
    with pytest.raises(ValidationError, match="restore_as_replacement_requires_candidate"):
        RestoreMemoryCommand(
            **common,
            target=target,
            mode=RestoreMode.AS_REPLACEMENT,
        )
    with pytest.raises(ValidationError, match="archived_restore_cannot_include"):
        RestoreMemoryCommand(
            **common,
            target=target,
            replacement_candidate=normal_goal_candidate,
        )
