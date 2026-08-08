from __future__ import annotations

import sqlite3
from uuid import UUID

import pytest

from app.services.memory.adapters import (
    CandidateReviewAction,
    CandidateReviewAdapter,
    ChatMemoryAdapter,
    GenericMemoryAdapter,
    ImportMemoryAdapter,
    TypedMemoryAdapter,
)
from app.services.memory.contracts import (
    MemoryOutcome,
    ReplacementAuthority,
    SourceKind,
    TargetRevision,
)
from app.services.memory.idempotency import MemoryIdempotency
from tests.memory.mutation_helpers import OWNER_A, memory_harness, video_goal

OLD_GOAL = "create long-form cinematic YouTube videos"
NEW_GOAL = "create short Instagram reels clearly"


def _state(path) -> dict[str, object]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        records = connection.execute(
            "SELECT memory_type, domain_key, slot_key, cardinality, canonical_payload, "
            "display_text, status, revision FROM memory_records "
            "ORDER BY CASE status WHEN 'superseded' THEN 0 ELSE 1 END, created_at, id"
        ).fetchall()
        return {
            "records": [tuple(row) for row in records],
            "relations": [
                tuple(row)
                for row in connection.execute(
                    "SELECT relation_type FROM memory_relations ORDER BY relation_type"
                ).fetchall()
            ],
            "operations": [
                tuple(row)
                for row in connection.execute(
                    "SELECT operation_kind, outcome FROM memory_operations ORDER BY created_at"
                ).fetchall()
            ],
            "sources": connection.execute("SELECT count(*) FROM memory_sources").fetchone()[0],
            "tombstones": connection.execute("SELECT count(*) FROM memory_tombstones").fetchone()[
                0
            ],
            "outbox": [
                tuple(row)
                for row in connection.execute(
                    "SELECT event_kind, state FROM memory_outbox ORDER BY event_kind, id"
                ).fetchall()
            ],
        }
    finally:
        connection.close()


def test_equivalent_create_through_generic_typed_chat_and_import_has_same_state(tmp_path) -> None:
    states = []
    outcomes = []
    for surface in ("generic", "typed", "chat", "import"):
        harness = memory_harness(
            tmp_path / surface,
            profile_id=f"disposable-{surface}",
            source_kind=(
                SourceKind.AUTOMATIC_EXTRACTION if surface == "chat" else SourceKind.MANUAL_UI
            ),
            message_id="message-1" if surface == "chat" else None,
        )
        item = video_goal(NEW_GOAL)
        if surface == "generic":
            result = GenericMemoryAdapter(harness.coordinator).create(
                harness.context,
                item,
                idempotency_key=MemoryIdempotency.http(OWNER_A, "request-1", "create"),
            )
        elif surface == "typed":
            result = TypedMemoryAdapter(harness.coordinator).create_typed(
                harness.context,
                item,
                client_mutation_id="typed-1",
            )
        elif surface == "chat":
            result = ChatMemoryAdapter(harness.coordinator).apply_structured_candidate(
                harness.context,
                item,
                extraction_version="legacy-structured-v1",
                candidate_key="candidate-0",
                transport="sync",
            )
        else:
            result = ImportMemoryAdapter(harness.coordinator).accept(
                harness.context,
                {
                    "owner_id": "foreign-owner-is-ignored",
                    "id": "foreign-canonical-id-is-ignored",
                    "status": "active",
                    "memory_type": "goal",
                    "domain_key": "video_creation",
                    "slot_key": "goal:video_creation:current_primary_goal",
                    "cardinality": "exclusive",
                    "canonical_value": NEW_GOAL,
                    "display_text": NEW_GOAL,
                },
                batch_id="batch-1",
                item_hash="item-hash-1",
            )
        assert result.mutation is not None
        outcomes.append(result.mutation.outcome.value)
        state = _state(harness.database_path)
        states.append(
            {
                "records": state["records"],
                "relations": state["relations"],
                "tombstones": state["tombstones"],
                "outbox": state["outbox"],
            }
        )

    assert outcomes == ["created"] * 4
    assert states[1:] == [states[0]] * 3


def test_sync_stream_and_cross_transport_retries_are_one_logical_operation(tmp_path) -> None:
    harness = memory_harness(
        tmp_path,
        source_kind=SourceKind.AUTOMATIC_EXTRACTION,
        message_id="message-sync-stream",
    )
    adapter = ChatMemoryAdapter(harness.coordinator)
    item = video_goal(NEW_GOAL)
    results = [
        adapter.apply_structured_candidate(
            harness.context,
            item,
            extraction_version="legacy-structured-v1",
            candidate_key="candidate-0",
            transport=transport,
        )
        for transport in ("sync", "sync", "stream", "stream", "sync")
    ]
    mutations = [result.mutation for result in results]
    assert all(result is not None for result in mutations)
    assert {result.operation_id for result in mutations if result is not None}.__len__() == 1
    assert {result.outcome for result in mutations if result is not None} == {MemoryOutcome.CREATED}

    state = _state(harness.database_path)
    assert len(state["records"]) == 1
    assert len(state["operations"]) == 1
    assert state["sources"] == 1
    assert len(state["outbox"]) == 1


@pytest.mark.parametrize("surface", ["generic", "typed", "review", "chat"])
def test_equivalent_replacement_through_four_surfaces_has_same_canonical_result(
    tmp_path,
    surface: str,
) -> None:
    harness = memory_harness(
        tmp_path / surface,
        profile_id=f"replacement-{surface}",
        source_kind=(
            SourceKind.AUTOMATIC_EXTRACTION if surface == "chat" else SourceKind.MANUAL_UI
        ),
        message_id="replacement-message" if surface == "chat" else None,
    )
    generic = GenericMemoryAdapter(harness.coordinator)
    created = generic.create(
        harness.context,
        video_goal(OLD_GOAL),
        idempotency_key=MemoryIdempotency.manual(OWNER_A, f"old-{surface}"),
    )
    assert created.mutation and str(created.mutation.active_memory_ids[-1])
    target = TargetRevision(
        memory_id=UUID(str(created.mutation.active_memory_ids[-1])),
        expected_revision=created.mutation.current_revision or 1,
    )
    item = video_goal(NEW_GOAL)
    if surface == "generic":
        result = generic.replace(
            harness.context,
            item,
            (target,),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            idempotency_key=MemoryIdempotency.http(OWNER_A, "replacement", "replace"),
        )
    elif surface == "typed":
        result = TypedMemoryAdapter(harness.coordinator).replace(
            harness.context,
            item,
            (target,),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            idempotency_key=MemoryIdempotency.manual(OWNER_A, "typed-replacement"),
        )
    elif surface == "review":
        reviewed = CandidateReviewAdapter(harness.coordinator).apply(
            harness.context,
            candidate_id="candidate-replacement",
            candidate_revision=1,
            action=CandidateReviewAction.REPLACE,
            item=item,
            targets=(target,),
            authority=ReplacementAuthority.REVIEWED,
        )
        assert reviewed.coordination is not None
        result = reviewed.coordination
    else:
        result = ChatMemoryAdapter(harness.coordinator).apply_structured_replacement(
            harness.context,
            item,
            (target,),
            extraction_version="legacy-structured-v1",
            candidate_key="replacement-candidate",
            transport="stream",
        )

    assert result.mutation is not None
    assert result.mutation.outcome.value == "replaced"
    state = _state(harness.database_path)
    assert [record[6] for record in state["records"]] == ["superseded", "active"]
    assert state["records"][1][1:6] == (
        "video_creation",
        "goal:video_creation:current_primary_goal",
        "exclusive",
        f'"{NEW_GOAL}"',
        NEW_GOAL,
    )
    assert state["relations"] == [("supersedes",)]


def test_candidate_review_rejection_and_ambiguity_do_not_mutate_canonical_state(tmp_path) -> None:
    harness = memory_harness(tmp_path)
    adapter = CandidateReviewAdapter(harness.coordinator)
    rejected = adapter.apply(
        harness.context,
        candidate_id="candidate-1",
        candidate_revision=1,
        action=CandidateReviewAction.REJECT,
    )
    ambiguous = adapter.apply(
        harness.context,
        candidate_id="candidate-2",
        candidate_revision=1,
        action=CandidateReviewAction.AMBIGUOUS,
    )
    assert rejected.outcome == "rejected"
    assert not rejected.review_required
    assert ambiguous.outcome == "needs_review"
    assert ambiguous.review_required
    assert not harness.database_path.exists()
