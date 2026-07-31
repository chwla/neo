#!/usr/bin/env python3
"""Disposable manual validation harness for the isolated Phase 2 memory kernel.

MANUAL TEST ONLY. The deterministic crypto provider imported below is deliberately
test-scoped and must never be used for production data.
"""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root below.

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.memory_v2_migrations import upgrade_memory_v2
from app.db.session import build_engine
from app.models.memory_v2 import (
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
)
from app.services.memory_v2.contracts import (
    ArchiveMemoryCommand,
    CandidateIntent,
    CreateMemoryCommand,
    ForgetMemoryCommand,
    MemoryErrorCode,
    MemoryOutcome,
    MemoryRejectionCode,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    RestoreMemoryCommand,
    TargetRevision,
)
from app.services.memory_v2.mutations import MemoryMutationService, RetryPolicy
from tests.memory_v2.helpers import (
    OWNER_A,
    DeterministicTestCrypto,
    actor,
    candidate,
    source,
)

DATABASE_IDENTITY = "manual-memory-v2-phase2-disposable"


def _service(engine) -> MemoryMutationService:
    provider = DeterministicTestCrypto()
    return MemoryMutationService(
        engine,
        owner_id=OWNER_A,
        database_identity=DATABASE_IDENTITY,
        payload_provider=provider,
        fingerprint_provider=provider,
        tombstone_provider=provider,
        key_versions=provider,
        retry_policy=RetryPolicy(attempts=6, base_delay_seconds=0.002),
    )


def _create(
    service: MemoryMutationService,
    value: str,
    key: str,
    *,
    domain: str,
    slot: str,
    explicit: bool = False,
) -> tuple[CreateMemoryCommand, object]:
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key=key,
        actor=actor(),
        source=source(),
        candidate=candidate(
            value,
            domain=domain,
            slot=slot,
            explicit=explicit,
        ),
    )
    return command, service.execute(command)


def _print_result(label: str, result) -> None:
    active = ",".join(str(item) for item in result.active_memory_ids) or "-"
    code = result.rejection_code or result.error_code or "-"
    print(
        f"RESULT {label}: outcome={result.outcome.value} operation={result.operation.value} "
        f"revision={result.current_revision} active={active} code={code}"
    )


def _critical_replacement(service: MemoryMutationService) -> None:
    slot = "goal:video_creation:current_primary_goal"
    create_command, created = _create(
        service,
        "create long-form cinematic YouTube videos",
        "manual-critical-create",
        domain="video_creation",
        slot=slot,
    )
    _print_result("critical create", created)
    predecessor = created.active_memory_ids[0]
    replacement_command = ReplaceMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="manual-critical-replace",
        actor=actor(),
        source=source(include_correction_evidence=True),
        candidate=candidate(
            "create short Instagram reels clearly",
            intent=CandidateIntent.REPLACE,
            targets=(predecessor,),
        ),
        authority=ReplacementAuthority.EXPLICIT_CORRECTION,
        targets=(TargetRevision(memory_id=predecessor, expected_revision=1),),
    )
    replaced = service.execute(replacement_command)
    replayed = service.execute(replacement_command)
    conflict_command = replacement_command.model_copy(
        update={
            "candidate": candidate(
                "create short tutorial reels clearly",
                intent=CandidateIntent.REPLACE,
                targets=(predecessor,),
            )
        }
    )
    conflict = service.execute(conflict_command)
    _print_result("critical replace", replaced)
    _print_result("exact replay", replayed)
    _print_result("idempotency conflict", conflict)

    assert replaced.outcome is MemoryOutcome.REPLACED
    assert replayed == replaced
    assert conflict.error_code is MemoryErrorCode.IDEMPOTENCY_CONFLICT
    with Session(service._engine) as session:  # manual inspection harness only
        active = session.scalar(
            select(MemoryRecordV2).where(
                MemoryRecordV2.owner_id == OWNER_A,
                MemoryRecordV2.slot_key == slot,
                MemoryRecordV2.status == "active",
            )
        )
        old = session.get(MemoryRecordV2, str(predecessor))
        assert active is not None
        assert active.canonical_payload == "create short Instagram reels clearly"
        assert active.display_text == "create short Instagram reels clearly"
        assert active.domain_key == "video_creation" and active.slot_key == slot
        assert old is not None and old.status == "superseded"
        relations = session.scalars(
            select(MemoryRelationV2).where(
                MemoryRelationV2.owner_id == OWNER_A,
                MemoryRelationV2.from_memory_id == active.id,
                MemoryRelationV2.to_memory_id == old.id,
                MemoryRelationV2.relation_type == "supersedes",
            )
        ).all()
        assert len(relations) == 1
        roles = set(
            session.scalars(
                select(MemorySourceV2.assertion_role).where(
                    MemorySourceV2.owner_id == OWNER_A,
                    MemorySourceV2.operation_id == str(replaced.operation_id),
                )
            )
        )
        assert {"supports", "retracts_predecessor"} <= roles
        critical_events = list(
            session.scalars(
                select(MemoryOutboxV2.event_kind).where(
                    MemoryOutboxV2.event_idempotency_key.contains(str(replaced.operation_id))
                )
            )
        )
        assert critical_events.count("canonical_remove") == 1
        assert critical_events.count("canonical_upsert") == 1
    del create_command


def _forget_and_resurrection(service: MemoryMutationService) -> None:
    slot = "goal:learning:current_primary_goal"
    _, created = _create(
        service,
        "study distributed systems",
        "manual-forget-create",
        domain="learning",
        slot=slot,
    )
    forgotten = service.execute(
        ForgetMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="manual-forget",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=created.active_memory_ids[0], expected_revision=1),
        )
    )
    _, blocked = _create(
        service,
        "study distributed systems",
        "manual-resurrection-blocked",
        domain="learning",
        slot=slot,
    )
    _print_result("forget", forgotten)
    _print_result("automatic resurrection", blocked)
    assert forgotten.outcome is MemoryOutcome.FORGOTTEN
    assert blocked.rejection_code is MemoryRejectionCode.RESURRECTION_BLOCKED


def _unsafe_restore(service: MemoryMutationService) -> None:
    slot = "goal:finance:current_primary_goal"
    _, historical = _create(
        service,
        "learn personal budgeting",
        "manual-restore-historical",
        domain="finance",
        slot=slot,
    )
    archived = service.execute(
        ArchiveMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="manual-restore-archive",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=historical.active_memory_ids[0], expected_revision=1),
        )
    )
    _, current = _create(
        service,
        "build a long-term investing plan",
        "manual-restore-current",
        domain="finance",
        slot=slot,
    )
    unsafe = service.execute(
        RestoreMemoryCommand(
            owner_id=OWNER_A,
            idempotency_key="manual-unsafe-restore",
            actor=actor(),
            source=source(),
            target=TargetRevision(memory_id=historical.active_memory_ids[0], expected_revision=2),
        )
    )
    _print_result("archive", archived)
    _print_result("current replacement slot create", current)
    _print_result("unsafe restore", unsafe)
    assert unsafe.rejection_code is MemoryRejectionCode.INVALID_RESTORE


def _concurrency(database_url: str) -> None:
    first_engine = build_engine(database_url)
    second_engine = build_engine(database_url)
    first_service = _service(first_engine)
    second_service = _service(second_engine)
    barrier = Barrier(2)

    def run(service: MemoryMutationService, command: CreateMemoryCommand):
        barrier.wait(timeout=5)
        return service.execute(command)

    race_slot = "goal:health_fitness:current_primary_goal"
    first = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="manual-concurrent-race-one",
        actor=actor(),
        source=source(),
        candidate=candidate("train for a trail race", domain="health_fitness", slot=race_slot),
    )
    second = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="manual-concurrent-race-two",
        actor=actor(),
        source=source(),
        candidate=candidate("build a strength routine", domain="health_fitness", slot=race_slot),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(run, first_service, first)
        two = pool.submit(run, second_service, second)
        results = (one.result(timeout=15), two.result(timeout=15))
    for index, result in enumerate(results, start=1):
        _print_result(f"concurrent {index}", result)
    assert {item.outcome for item in results} == {
        MemoryOutcome.CREATED,
        MemoryOutcome.NEEDS_REVIEW,
    }
    with Session(first_engine) as session:
        active = session.scalars(
            select(MemoryRecordV2).where(
                MemoryRecordV2.slot_key == race_slot,
                MemoryRecordV2.status == "active",
            )
        ).all()
        assert len(active) == 1
    first_engine.dispose()
    second_engine.dispose()


def _print_database(engine) -> None:
    print("\nCANONICAL RECORDS")
    with Session(engine) as session:
        for row in session.scalars(select(MemoryRecordV2).order_by(MemoryRecordV2.created_at)):
            value = (
                row.display_text if row.display_text is not None else "<payload-erased-or-opaque>"
            )
            print(
                f"  {row.id} status={row.status} rev={row.revision} "
                f"type={row.memory_type} domain={row.domain_key} "
                f"slot={row.slot_key} value={value!r}"
            )
        print("RELATIONS")
        for row in session.scalars(select(MemoryRelationV2).order_by(MemoryRelationV2.created_at)):
            print(f"  {row.relation_type}: {row.from_memory_id} -> {row.to_memory_id}")
        print("PROVENANCE")
        for row in session.scalars(select(MemorySourceV2).order_by(MemorySourceV2.created_at)):
            excerpt = row.redacted_excerpt or "<none-or-opaque>"
            print(
                f"  memory={row.memory_id} role={row.assertion_role} "
                f"kind={row.source_kind} evidence={excerpt[:80]!r}"
            )
        print("TOMBSTONES")
        tombstones = select(MemoryTombstoneV2).order_by(MemoryTombstoneV2.created_at)
        for row in session.scalars(tombstones):
            print(
                f"  {row.id} owner={row.owner_id} digest={row.fingerprint_digest[:16]}... "
                f"expires={row.expires_at.isoformat()} reconfirmed={row.explicitly_reconfirmed}"
            )
        print("OPERATIONS")
        operations = select(MemoryOperationV2).order_by(MemoryOperationV2.created_at)
        for row in session.scalars(operations):
            print(
                f"  {row.id} key={row.idempotency_key} kind={row.operation_kind} "
                f"status={row.status} outcome={row.outcome} results={row.result_record_ids}"
            )
        print("OUTBOX")
        for row in session.scalars(select(MemoryOutboxV2).order_by(MemoryOutboxV2.created_at)):
            print(
                f"  {row.id} kind={row.event_kind} state={row.state} "
                f"memory={row.memory_id or '-'} revision={row.canonical_revision} "
                f"key={row.event_idempotency_key}"
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        help="Explicit new SQLite file. Refused if it already exists.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the automatically-created temporary directory for SQL inspection.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    owned_temp_dir: Path | None = None
    if args.database is not None:
        database_path = args.database.expanduser().resolve()
        if database_path.exists():
            print(f"REFUSING existing database path: {database_path}", file=sys.stderr)
            return 2
        if not database_path.parent.exists():
            print("Database parent directory must already exist.", file=sys.stderr)
            return 2
    else:
        owned_temp_dir = Path(tempfile.mkdtemp(prefix="neo-memory-v2-phase2-"))
        database_path = owned_temp_dir / "manual-phase2.sqlite3"

    database_url = f"sqlite:///{database_path}"
    print(f"DISPOSABLE DATABASE: {database_path}")
    engine = build_engine(database_url)
    try:
        upgrade_memory_v2(
            engine,
            owner_id=OWNER_A,
            database_identity=DATABASE_IDENTITY,
        )
        service = _service(engine)
        _critical_replacement(service)
        _forget_and_resurrection(service)
        _unsafe_restore(service)
        _concurrency(database_url)
        _print_database(engine)
        print("\nPASS: all Phase 2 manual invariants held")
        return 0
    except Exception as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()
        if owned_temp_dir is not None and not args.keep:
            shutil.rmtree(owned_temp_dir)
            print(f"CLEANED: {owned_temp_dir}")
        elif owned_temp_dir is not None:
            print(f"KEPT FOR INSPECTION: {owned_temp_dir}")
            print(f"CLEANUP WHEN DONE: rm -rf {owned_temp_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
