from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.memory import MemoryRecord, MemoryRelation
from app.services.memory.contracts import (
    CandidateIntent,
    CreateMemoryCommand,
    MemoryOutcome,
    MergeMemoryCommand,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    SupersedeMemoryCommand,
    TargetRevision,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.helpers import OWNER_A, actor, candidate, source


def _create(service, value: str, *, key: str, **kwargs):
    return service.execute(
        CreateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key=key,
            actor=actor(),
            source=source(),
            candidate=candidate(value, **kwargs),
        )
    )


def test_grounded_same_slot_structured_correction_is_deterministic(
    mutation_service,
    phase2_engine,
) -> None:
    original = _create(
        mutation_service,
        "create long-form cinematic YouTube videos",
        key="implicit-original",
    )
    replacement = mutation_service.execute(
        ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="implicit-structured-correction",
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                "create short Instagram reels clearly",
                intent=CandidateIntent.REPLACE,
            ),
            authority=ReplacementAuthority.GROUNDED_SAME_SLOT_ASSERTION,
        )
    )

    assert replacement.outcome is MemoryOutcome.REPLACED
    with Session(phase2_engine) as session:
        active = session.get(MemoryRecord, str(replacement.active_memory_ids[0]))
        old = session.get(MemoryRecord, str(original.active_memory_ids[0]))
        assert active is not None and old is not None
        assert active.domain_key == "video_creation"
        assert active.slot_key == old.slot_key
        assert active.canonical_payload == "create short Instagram reels clearly"
        assert old.status == "superseded"


def test_explicit_domain_change_rebuilds_semantic_slot(mutation_service, phase2_engine) -> None:
    original = _create(
        mutation_service,
        "create video tutorials",
        key="domain-change-original",
    )
    changed = mutation_service.execute(
        ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="domain-change-replace",
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                "build a structured learning curriculum",
                intent=CandidateIntent.REPLACE,
                domain="learning",
                slot="goal:learning:current_primary_goal",
                targets=(original.active_memory_ids[0],),
                explicit_domain_change=True,
            ),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(TargetRevision(memory_id=original.active_memory_ids[0], expected_revision=1),),
        )
    )
    assert changed.outcome is MemoryOutcome.REPLACED
    with Session(phase2_engine) as session:
        record = session.get(MemoryRecord, str(changed.active_memory_ids[0]))
        assert record is not None
        assert record.domain_key == "learning"
        assert record.slot_key == "goal:learning:current_primary_goal"


def test_category_correction_and_preference_scopes_remain_distinct(
    mutation_service,
    phase2_engine,
) -> None:
    mistaken_goal = _create(
        mutation_service,
        "give code advice as checklists",
        key="category-original",
    )
    corrected = mutation_service.execute(
        ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="category-correction",
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                "give code advice as checklists",
                memory_type=MemoryType.PREFERENCE,
                domain="software_development",
                slot="preference:software_development:advice_format",
                cardinality=Cardinality.EXCLUSIVE,
                intent=CandidateIntent.REPLACE,
                targets=(mistaken_goal.active_memory_ids[0],),
                explicit_domain_change=True,
                explicit_slot_change=True,
            ),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(
                TargetRevision(memory_id=mistaken_goal.active_memory_ids[0], expected_revision=1),
            ),
        )
    )
    global_style = _create(
        mutation_service,
        "use concise answers globally",
        key="global-style",
        memory_type=MemoryType.PREFERENCE,
        domain="global",
        slot="preference:global:format",
    )

    assert corrected.outcome is MemoryOutcome.REPLACED
    assert global_style.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        scoped = session.get(MemoryRecord, str(corrected.active_memory_ids[0]))
        global_record = session.get(MemoryRecord, str(global_style.active_memory_ids[0]))
        assert scoped is not None and global_record is not None
        assert scoped.memory_type == "preference"
        assert scoped.domain_key == "software_development"
        assert global_record.domain_key == "global"
        assert scoped.slot_key != global_record.slot_key


def test_one_replacement_can_clean_up_multiple_explicit_predecessors(
    mutation_service,
    phase2_engine,
) -> None:
    video = _create(mutation_service, "create videos", key="multi-video")
    learning = _create(
        mutation_service,
        "learn editing",
        key="multi-learning",
        domain="learning",
        slot="goal:learning:current_primary_goal",
    )
    targets = (
        TargetRevision(memory_id=video.active_memory_ids[0], expected_revision=1),
        TargetRevision(memory_id=learning.active_memory_ids[0], expected_revision=1),
    )
    replacement = mutation_service.execute(
        ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="multi-replacement",
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                "create short Instagram reels clearly",
                intent=CandidateIntent.REPLACE,
                targets=tuple(item.memory_id for item in targets),
            ),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=targets,
        )
    )

    assert replacement.outcome is MemoryOutcome.REPLACED
    with Session(phase2_engine) as session:
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "active")
            )
            == 1
        )
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "superseded")
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count(MemoryRelation.id)).where(
                    MemoryRelation.relation_type == "supersedes"
                )
            )
            == 2
        )


def test_explicit_compatible_merge_uses_supplied_value_without_text_concatenation(
    mutation_service,
    phase2_engine,
) -> None:
    slot_one = f"goal:learning:independent:{uuid4()}"
    slot_two = f"goal:learning:independent:{uuid4()}"
    first = _create(
        mutation_service,
        "study Python",
        key="merge-source-one",
        domain="learning",
        slot=slot_one,
        cardinality=Cardinality.ADDITIVE,
    )
    second = _create(
        mutation_service,
        "practice Python projects",
        key="merge-source-two",
        domain="learning",
        slot=slot_two,
        cardinality=Cardinality.ADDITIVE,
    )
    merged_value = "learn Python through study and practical projects"
    merged = mutation_service.execute(
        MergeMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="explicit-merge",
            actor=actor(),
            source=source(),
            sources=(
                TargetRevision(memory_id=first.active_memory_ids[0], expected_revision=1),
                TargetRevision(memory_id=second.active_memory_ids[0], expected_revision=1),
            ),
            candidate=candidate(
                merged_value,
                domain="learning",
                slot=f"goal:learning:independent:{uuid4()}",
                cardinality=Cardinality.ADDITIVE,
            ),
        )
    )
    assert merged.outcome is MemoryOutcome.MERGED
    with Session(phase2_engine) as session:
        record = session.get(MemoryRecord, str(merged.active_memory_ids[0]))
        assert record is not None and record.canonical_payload == merged_value
        assert "\n" not in record.canonical_payload
        assert (
            session.scalar(
                select(func.count(MemoryRelation.id)).where(
                    MemoryRelation.relation_type == "merged_from"
                )
            )
            == 2
        )


def test_explicit_supersede_uses_existing_active_successor(mutation_service, phase2_engine) -> None:
    predecessor = _create(
        mutation_service,
        "study introductory statistics",
        key="supersede-predecessor",
        domain="learning",
        slot=f"goal:learning:independent:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
    )
    successor = _create(
        mutation_service,
        "complete an applied statistics course",
        key="supersede-successor",
        domain="learning",
        slot=f"goal:learning:independent:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
    )
    result = mutation_service.execute(
        SupersedeMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="explicit-supersede",
            actor=actor(),
            source=source(),
            predecessors=(
                TargetRevision(memory_id=predecessor.active_memory_ids[0], expected_revision=1),
            ),
            successor_memory_id=successor.active_memory_ids[0],
        )
    )
    assert result.outcome is MemoryOutcome.SUPERSEDED
    assert result.active_memory_ids == successor.active_memory_ids
    with Session(phase2_engine) as session:
        old = session.get(MemoryRecord, str(predecessor.active_memory_ids[0]))
        assert old is not None and old.status == "superseded"
