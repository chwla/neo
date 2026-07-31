from __future__ import annotations

import sqlite3
from dataclasses import replace
from uuid import UUID, uuid4

from app.services.memory_v2.adapters import GenericMemoryV2Adapter
from app.services.memory_v2.contracts import (
    DetachMemorySourceCommand,
    SourceChangeOutcome,
    TargetRevision,
)
from app.services.memory_v2.idempotency import MemoryV2Idempotency
from app.services.memory_v2.source_changes import MemoryV2SourceChangeCoordinator
from tests.memory_v2.phase3_helpers import OWNER_A, OWNER_B, phase3_harness, video_goal


def _counts(path) -> tuple[int, int, int]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute("SELECT count(*) FROM memory_records_v2").fetchone()[0],
            connection.execute("SELECT count(*) FROM memory_tombstones_v2").fetchone()[0],
            connection.execute("SELECT count(*) FROM memory_operations_v2").fetchone()[0],
        )
    finally:
        connection.close()


def _source_state(path, memory_id: UUID) -> dict[str, object]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        record = dict(
            connection.execute(
                "SELECT id, owner_id, status, revision FROM memory_records_v2 WHERE id = ?",
                (str(memory_id),),
            ).fetchone()
        )
        sources = [
            dict(row)
            for row in connection.execute(
                "SELECT id, source_id, message_id, assertion_role, is_active, "
                "detachment_reason FROM memory_sources_v2 WHERE memory_id = ? ORDER BY id",
                (str(memory_id),),
            ).fetchall()
        ]
        operations = [
            row[0]
            for row in connection.execute(
                "SELECT operation_kind FROM memory_operations_v2 ORDER BY created_at, id"
            ).fetchall()
        ]
        outbox = [
            row[0]
            for row in connection.execute(
                "SELECT event_kind FROM memory_outbox_v2 ORDER BY created_at, id"
            ).fetchall()
        ]
        return {
            "record": record,
            "sources": sources,
            "operations": operations,
            "outbox": outbox,
        }
    finally:
        connection.close()


def _create_with_two_supports(harness):
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    first_context = replace(
        harness.context,
        source_id="support-a",
        message_id="message-a",
    )
    second_context = replace(
        harness.context,
        source_id="support-b",
        message_id="message-b",
    )
    created = adapter.create(
        first_context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "source-create-a"),
    )
    assert created.compatibility and created.compatibility.active_memory_id
    reconfirmed = adapter.create(
        second_context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "source-create-b"),
    )
    assert reconfirmed.compatibility and reconfirmed.compatibility.outcome == "reconfirmed"
    memory_id = UUID(created.compatibility.active_memory_id)
    state = _source_state(harness.database_path, memory_id)
    assert state["record"]["status"] == "active"
    assert state["record"]["revision"] == 2
    assert len(state["sources"]) == 2
    assert all(row["is_active"] == 1 for row in state["sources"])
    return adapter, memory_id, state


def test_archive_restore_forget_and_permanent_erasure_use_commands(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    created = adapter.create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "lifecycle-create"),
    )
    assert created.compatibility and created.compatibility.active_memory_id
    target = TargetRevision(
        memory_id=UUID(created.compatibility.active_memory_id),
        expected_revision=1,
    )
    archived = adapter.archive(
        harness.context,
        target,
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "archive"),
    )
    assert archived.compatibility and archived.compatibility.outcome == "archived"
    restored = adapter.restore(
        harness.context,
        TargetRevision(memory_id=target.memory_id, expected_revision=2),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "restore"),
    )
    assert restored.compatibility and restored.compatibility.outcome == "restored"
    forgotten = adapter.forget(
        harness.context,
        TargetRevision(memory_id=target.memory_id, expected_revision=3),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "forget"),
    )
    assert forgotten.compatibility and forgotten.compatibility.outcome == "forgotten"
    assert _counts(harness.database_path) == (1, 1, 4)

    second = adapter.create(
        harness.context,
        video_goal("create tutorial videos"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "erase-create"),
    )
    assert second.compatibility and second.compatibility.active_memory_id
    erased = adapter.erase_permanently(
        harness.context,
        TargetRevision(
            memory_id=UUID(second.compatibility.active_memory_id),
            expected_revision=1,
        ),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "erase"),
    )
    assert erased.compatibility and erased.compatibility.outcome == "erased_permanently"


def test_source_detach_preserves_memory_and_other_persisted_support_idempotently(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter, memory_id, before = _create_with_two_supports(harness)
    target = TargetRevision(memory_id=memory_id, expected_revision=2)
    detached_row = next(row for row in before["sources"] if row["message_id"] == "message-a")
    source_changes = MemoryV2SourceChangeCoordinator(adapter)

    first = source_changes.delete_message_source(
        harness.context,
        message_id="message-1",
        edit_revision=2,
        target=target,
        source_id=UUID(detached_row["id"]),
    )
    assert first.action.value == "detach_source"
    assert first.outcome is SourceChangeOutcome.PRESERVED
    assert first.review_required is False
    assert first.memory_id == memory_id
    assert first.detached_source_id == UUID(detached_row["id"])
    assert first.remaining_active_source_count == 1
    assert first.canonical_mutation_performed is False
    assert first.canonical_revision_changed is False
    assert first.memory_revision == 2

    persisted = _source_state(harness.database_path, memory_id)
    detached = next(row for row in persisted["sources"] if row["id"] == detached_row["id"])
    remaining = next(row for row in persisted["sources"] if row["id"] != detached_row["id"])
    assert detached["is_active"] == 0
    assert detached["detachment_reason"] == "source_deleted"
    assert remaining["is_active"] == 1
    assert remaining["assertion_role"] == "supports"
    assert persisted["record"]["status"] == "active"
    assert persisted["record"]["revision"] == 2
    assert persisted["operations"] == before["operations"] == ["create", "create"]
    assert "canonical_remove" not in persisted["outbox"]

    retry = source_changes.delete_message_source(
        harness.context,
        message_id="message-1",
        edit_revision=2,
        target=target,
        source_id=UUID(detached_row["id"]),
    )
    assert retry.outcome is SourceChangeOutcome.ALREADY_DETACHED
    assert retry.detached_source_id == first.detached_source_id
    assert retry.remaining_active_source_count == 1
    assert retry.review_required is False
    assert _source_state(harness.database_path, memory_id) == persisted


def test_final_source_detaches_and_requires_review_without_lifecycle_operation(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter, memory_id, before = _create_with_two_supports(harness)
    target = TargetRevision(memory_id=memory_id, expected_revision=2)
    source_changes = MemoryV2SourceChangeCoordinator(adapter)
    first_row, final_row = before["sources"]
    preserved = source_changes.delete_message_source(
        harness.context,
        message_id="message-a",
        edit_revision=1,
        target=target,
        source_id=UUID(first_row["id"]),
    )
    assert preserved.outcome is SourceChangeOutcome.PRESERVED
    final = source_changes.delete_message_source(
        harness.context,
        message_id="message-final",
        edit_revision=2,
        target=target,
        source_id=UUID(final_row["id"]),
    )
    assert final.outcome is SourceChangeOutcome.NEEDS_REVIEW
    assert final.review_required is True
    assert final.remaining_active_source_count == 0
    assert final.detached_source_id == UUID(final_row["id"])
    assert final.canonical_mutation_performed is False
    assert final.canonical_revision_changed is False

    persisted = _source_state(harness.database_path, memory_id)
    assert all(row["is_active"] == 0 for row in persisted["sources"])
    assert persisted["record"]["status"] == "active"
    assert persisted["record"]["revision"] == 2
    assert persisted["operations"] == ["create", "create"]
    assert not ({"archive", "forget", "supersede"} & set(persisted["operations"]))
    assert "canonical_remove" not in persisted["outbox"]
    assert _counts(harness.database_path) == (1, 0, 2)


def test_source_not_found_and_owner_mismatch_are_stable_typed_results(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter, memory_id, before = _create_with_two_supports(harness)
    target = TargetRevision(memory_id=memory_id, expected_revision=2)
    source_changes = MemoryV2SourceChangeCoordinator(adapter)

    missing = source_changes.delete_message_source(
        harness.context,
        message_id="missing-message",
        edit_revision=1,
        target=target,
        source_id=uuid4(),
    )
    assert missing.outcome is SourceChangeOutcome.SOURCE_NOT_FOUND
    assert missing.detached_source_id is None
    assert missing.review_required is False
    assert _source_state(harness.database_path, memory_id) == before

    source_id = UUID(before["sources"][0]["id"])
    mismatch = harness.coordinator.detach_source(
        harness.context.execution,
        DetachMemorySourceCommand(
            owner_id=OWNER_B,
            idempotency_key=MemoryV2Idempotency.source_change(
                OWNER_B, "owner-mismatch", 1, str(memory_id), "delete"
            ),
            target=target,
            source_id=source_id,
        ),
    )
    assert mismatch.outcome is SourceChangeOutcome.OWNER_MISMATCH
    assert mismatch.detached_source_id is None
    assert mismatch.review_required is False
    assert _source_state(harness.database_path, memory_id) == before
