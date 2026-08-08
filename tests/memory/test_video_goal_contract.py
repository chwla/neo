from __future__ import annotations

from uuid import UUID

from app.services.memory.contracts import (
    ActorKind,
    CandidateIntent,
    CandidateTargetHints,
    EvidenceRole,
    EvidenceSpan,
    MemoryActor,
    MemorySource,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    Sensitivity,
    SourceKind,
    ValidatedCandidateProposal,
)
from app.services.memory.taxonomy import (
    Cardinality,
    MemoryIdentity,
    MemoryType,
    build_slot,
    inherit_predecessor_identity,
    resolve_domain,
)

OLD_GOAL = "create long-form cinematic YouTube videos"
CORRECTION = (
    "I no longer want to make long-form cinematic YouTube videos. "
    "I want to create short Instagram reels clearly."
)
NEW_GOAL = "create short Instagram reels clearly"


def test_critical_video_goal_correction_has_clean_positive_replacement_contract() -> None:
    old_domain = resolve_domain(OLD_GOAL)
    old_slot = build_slot(
        MemoryType.GOAL,
        old_domain.key,
        goal_role="primary_output",
    )
    predecessor = MemoryIdentity(
        memory_type=MemoryType.GOAL,
        domain_key=old_slot.domain_key,
        slot_key=old_slot.slot_key,
        cardinality=old_slot.cardinality,
    )
    inherited = inherit_predecessor_identity(predecessor)

    proposed_domain = resolve_domain(CORRECTION)
    assert proposed_domain.key == "video_creation"
    assert inherited.domain_key == "video_creation"
    assert inherited.slot_key == "goal:video_creation:primary_output"
    assert inherited.cardinality is Cardinality.EXCLUSIVE

    candidate = ValidatedCandidateProposal(
        proposal_id=UUID("00000000-0000-4000-8000-000000000501"),
        intent=CandidateIntent.REPLACE,
        memory_type=MemoryType.GOAL,
        domain_key=inherited.domain_key,
        slot_key=inherited.slot_key,
        cardinality=inherited.cardinality,
        canonical_value=NEW_GOAL,
        display_text=NEW_GOAL,
        sensitivity=Sensitivity.NORMAL,
        confidence=0.99,
        importance=8,
        target_hints=CandidateTargetHints(
            old_value_phrases=("make long-form cinematic YouTube videos",),
            predecessor_domain_key=predecessor.domain_key,
            predecessor_slot_key=predecessor.slot_key,
        ),
        evidence=(
            EvidenceSpan(
                role=EvidenceRole.RETRACTION,
                text="I no longer want to make long-form cinematic YouTube videos",
            ),
            EvidenceSpan(
                role=EvidenceRole.ASSERTION,
                text="I want to create short Instagram reels clearly",
            ),
        ),
    )
    command = ReplaceMemoryCommand(
        owner_id="00000000-0000-4000-8000-000000000001",
        idempotency_key="chat:correction-message:goal",
        actor=MemoryActor(kind=ActorKind.USER, actor_id="user-1"),
        source=MemorySource(kind=SourceKind.CHAT_MESSAGE, message_id="message-2"),
        candidate=candidate,
        authority=ReplacementAuthority.LINKED_RETRACTION_REPLACEMENT,
    )

    payload = command.model_dump(mode="json")
    assert payload["operation"] == "replace"
    assert payload["candidate"]["canonical_value"] == NEW_GOAL
    assert payload["candidate"]["display_text"] == NEW_GOAL
    assert "no longer" not in payload["candidate"]["display_text"].lower()
    assert "not long-form" not in payload["candidate"]["display_text"].lower()
    assert payload["candidate"]["domain_key"] != "clearly"
    assert payload["candidate"]["target_hints"]["old_value_phrases"] == [
        "make long-form cinematic YouTube videos"
    ]
