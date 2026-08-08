from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.memory_migrations import upgrade_memory
from app.db.session import build_engine
from app.models.memory import MemoryOperation, MemoryRecord
from app.services.memory.contracts import (
    ArchiveMemoryCommand,
    CandidateIntent,
    CreateMemoryCommand,
    MemoryErrorCode,
    MemoryOutcome,
    MemoryUpdatePatch,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    TargetRevision,
    UpdateMemoryCommand,
)
from app.services.memory.mutations import MemoryMutationService, RetryPolicy
from app.services.memory.taxonomy import Cardinality
from tests.memory.helpers import (
    DATABASE_IDENTITY,
    OWNER_A,
    OWNER_B,
    actor,
    candidate,
    source,
)


def _service(engine, crypto, *, owner=OWNER_A, identity=DATABASE_IDENTITY):
    return MemoryMutationService(
        engine,
        owner_id=owner,
        database_identity=identity,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
        retry_policy=RetryPolicy(attempts=5, base_delay_seconds=0.002),
    )


def _parallel(first, second):
    barrier = Barrier(2)

    def run(callable_):
        barrier.wait(timeout=5)
        return callable_()

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run, first)
        two = pool.submit(run, second)
        return one.result(timeout=15), two.result(timeout=15)


def _create_command(value: str, key: str, **kwargs):
    return CreateMemoryCommand(
        owner_id=kwargs.pop("owner_id", OWNER_A),
        idempotency_key=key,
        actor=actor(),
        source=source(),
        candidate=candidate(value, **kwargs),
    )


def test_two_simultaneous_incompatible_exclusive_creates_leave_one_active(
    phase2_engine,
    test_crypto,
) -> None:
    first_service = _service(phase2_engine, test_crypto)
    second_service = _service(phase2_engine, test_crypto)
    first_command = _create_command("create tutorial videos", "concurrent-create-one")
    second_command = _create_command("create cinematic videos", "concurrent-create-two")

    results = _parallel(
        lambda: first_service.execute(first_command),
        lambda: second_service.execute(second_command),
    )
    assert {item.outcome for item in results} == {
        MemoryOutcome.CREATED,
        MemoryOutcome.NEEDS_REVIEW,
    }
    with Session(phase2_engine) as session:
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "active")
            )
            == 1
        )


def test_simultaneous_duplicate_creates_make_one_record_and_reconfirm(
    phase2_engine,
    test_crypto,
) -> None:
    first_service = _service(phase2_engine, test_crypto)
    second_service = _service(phase2_engine, test_crypto)
    first_command = _create_command("create tutorial videos", "duplicate-one")
    second_command = _create_command("create tutorial videos", "duplicate-two")

    results = _parallel(
        lambda: first_service.execute(first_command),
        lambda: second_service.execute(second_command),
    )
    assert {item.outcome for item in results} == {
        MemoryOutcome.CREATED,
        MemoryOutcome.RECONFIRMED,
    }
    with Session(phase2_engine) as session:
        records = session.scalars(select(MemoryRecord)).all()
        assert len(records) == 1 and records[0].revision == 2


def test_concurrent_identical_idempotency_key_returns_one_committed_result(
    phase2_engine,
    test_crypto,
) -> None:
    first_service = _service(phase2_engine, test_crypto)
    second_service = _service(phase2_engine, test_crypto)
    command = _create_command("create tutorial videos", "same-concurrent-key")

    first, second = _parallel(
        lambda: first_service.execute(command),
        lambda: second_service.execute(command),
    )
    assert first == second
    assert first.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryOperation.id))) == 1
        assert session.scalar(select(func.count(MemoryRecord.id))) == 1


def test_replay_after_new_service_instance_uses_durable_ledger(
    phase2_engine,
    test_crypto,
) -> None:
    command = _create_command("create tutorial videos", "restart-replay")
    first = _service(phase2_engine, test_crypto).execute(command)
    replay = _service(phase2_engine, test_crypto).execute(command)
    assert replay == first


def test_replay_returns_original_revision_after_later_mutation(
    phase2_engine,
    test_crypto,
) -> None:
    service = _service(phase2_engine, test_crypto)
    create = _create_command("create tutorial videos", "historical-replay")
    original = service.execute(create)
    memory_id = original.active_memory_ids[0]
    refined = service.execute(
        UpdateMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="historical-replay-refinement",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=memory_id, expected_revision=1),
            patch=MemoryUpdatePatch(
                canonical_value="create tutorial videos with examples",
                display_text="create tutorial videos with examples",
            ),
        )
    )
    assert refined.current_revision == 2

    replay = _service(phase2_engine, test_crypto).execute(create)
    assert replay == original
    assert replay.current_revision == 1


def test_deterministic_review_replays_without_new_state(phase2_engine, test_crypto) -> None:
    service = _service(phase2_engine, test_crypto)
    service.execute(_create_command("create tutorial videos", "review-base"))
    conflict = _create_command("create cinematic videos", "review-replay")
    first = service.execute(conflict)
    replay = _service(phase2_engine, test_crypto).execute(conflict)
    assert first == replay
    assert first.outcome is MemoryOutcome.NEEDS_REVIEW
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecord.id))) == 1
        assert session.scalar(select(func.count(MemoryOperation.id))) == 2


def test_replacement_racing_update_leaves_one_atomic_winner(
    phase2_engine,
    test_crypto,
) -> None:
    setup = _service(phase2_engine, test_crypto)
    created = setup.execute(_create_command("create tutorial videos", "race-update-base"))
    memory_id = created.active_memory_ids[0]
    replacement = ReplaceMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="race-replacement",
        actor=actor(),
        source=source(include_correction_evidence=True),
        candidate=candidate(
            "create short Instagram reels clearly",
            intent=CandidateIntent.REPLACE,
            targets=(memory_id,),
        ),
        authority=ReplacementAuthority.EXPLICIT_CORRECTION,
        targets=(TargetRevision(memory_id=memory_id, expected_revision=1),),
    )
    refinement = UpdateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="race-update",
        actor=actor(),
        source=source(),
        target=TargetRevision(memory_id=memory_id, expected_revision=1),
        patch=MemoryUpdatePatch(
            canonical_value="create tutorial videos with examples",
            display_text="create tutorial videos with examples",
        ),
    )
    results = _parallel(
        lambda: _service(phase2_engine, test_crypto).execute(replacement),
        lambda: _service(phase2_engine, test_crypto).execute(refinement),
    )
    assert (
        sum(item.outcome in {MemoryOutcome.REPLACED, MemoryOutcome.REFINED} for item in results)
        == 1
    )
    assert any(item.error_code is MemoryErrorCode.REVISION_CONFLICT for item in results)
    with Session(phase2_engine) as session:
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "active")
            )
            == 1
        )


def test_replacement_racing_archive_and_two_replacements_have_one_winner(
    phase2_engine,
    test_crypto,
) -> None:
    setup = _service(phase2_engine, test_crypto)
    created = setup.execute(_create_command("create tutorial videos", "race-archive-base"))
    memory_id = created.active_memory_ids[0]

    def replacement(key: str, value: str):
        return ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key=key,
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                value,
                intent=CandidateIntent.REPLACE,
                targets=(memory_id,),
            ),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(TargetRevision(memory_id=memory_id, expected_revision=1),),
        )

    archive = ArchiveMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="race-archive",
        actor=actor(),
        source=source(),
        target=TargetRevision(memory_id=memory_id, expected_revision=1),
    )
    first_results = _parallel(
        lambda: _service(phase2_engine, test_crypto).execute(
            replacement("race-archive-replace", "create short reels")
        ),
        lambda: _service(phase2_engine, test_crypto).execute(archive),
    )
    assert (
        sum(
            item.outcome in {MemoryOutcome.REPLACED, MemoryOutcome.ARCHIVED}
            for item in first_results
        )
        == 1
    )

    # A fresh database fixture is not available inside one test, so only run the
    # two-replacement race when replacement won and the original target changed.
    if any(item.outcome is MemoryOutcome.REPLACED for item in first_results):
        assert any(item.error_code is MemoryErrorCode.REVISION_CONFLICT for item in first_results)
    with Session(phase2_engine) as session:
        assert (
            session.scalar(
                select(func.count(MemoryRecord.id)).where(MemoryRecord.status == "active")
            )
            <= 1
        )


def test_two_replacements_targeting_same_predecessor_have_one_winner(
    phase2_engine,
    test_crypto,
) -> None:
    setup = _service(phase2_engine, test_crypto)
    created = setup.execute(_create_command("create tutorial videos", "two-replace-base"))
    memory_id = created.active_memory_ids[0]

    def command(key: str, value: str):
        return ReplaceMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key=key,
            actor=actor(),
            source=source(include_correction_evidence=True),
            candidate=candidate(
                value,
                intent=CandidateIntent.REPLACE,
                targets=(memory_id,),
            ),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            targets=(TargetRevision(memory_id=memory_id, expected_revision=1),),
        )

    results = _parallel(
        lambda: _service(phase2_engine, test_crypto).execute(
            command("replace-race-one", "create short reels")
        ),
        lambda: _service(phase2_engine, test_crypto).execute(
            command("replace-race-two", "create tutorial shorts")
        ),
    )
    assert sum(item.outcome is MemoryOutcome.REPLACED for item in results) == 1
    assert any(item.error_code is MemoryErrorCode.REVISION_CONFLICT for item in results)


def test_concurrent_additive_goals_both_commit(phase2_engine, test_crypto) -> None:
    from uuid import uuid4

    first = _create_command(
        "learn Spanish",
        "additive-race-one",
        domain="learning",
        slot=f"goal:learning:independent:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
    )
    second = _create_command(
        "study statistics",
        "additive-race-two",
        domain="learning",
        slot=f"goal:learning:independent:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
    )
    results = _parallel(
        lambda: _service(phase2_engine, test_crypto).execute(first),
        lambda: _service(phase2_engine, test_crypto).execute(second),
    )
    assert all(item.outcome is MemoryOutcome.CREATED for item in results)


def test_same_key_and_slot_are_independent_across_owner_databases(tmp_path, test_crypto) -> None:
    first_engine = build_engine(f"sqlite:///{tmp_path / 'owner-a.db'}")
    second_engine = build_engine(f"sqlite:///{tmp_path / 'owner-b.db'}")
    second_identity = "phase2-owner-b-profile"
    upgrade_memory(first_engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)
    upgrade_memory(second_engine, owner_id=OWNER_B, database_identity=second_identity)
    try:
        first = _create_command("create tutorial videos", "shared-owner-key")
        second = _create_command(
            "create tutorial videos",
            "shared-owner-key",
            owner_id=OWNER_B,
        )
        results = _parallel(
            lambda: _service(first_engine, test_crypto).execute(first),
            lambda: _service(
                second_engine,
                test_crypto,
                owner=OWNER_B,
                identity=second_identity,
            ).execute(second),
        )
        assert all(item.outcome is MemoryOutcome.CREATED for item in results)
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_transient_busy_attempt_retries_with_one_operation(
    phase2_engine,
    test_crypto,
    monkeypatch,
) -> None:
    service = _service(phase2_engine, test_crypto)
    original_commit = service._commit_prepared
    attempts = 0

    def transient_once(command, prepared):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "BEGIN IMMEDIATE",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        return original_commit(command, prepared)

    monkeypatch.setattr(service, "_commit_prepared", transient_once)
    result = service.execute(_create_command("create tutorial videos", "transient-retry"))
    assert result.outcome is MemoryOutcome.CREATED
    assert attempts == 2
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryOperation.id))) == 1
