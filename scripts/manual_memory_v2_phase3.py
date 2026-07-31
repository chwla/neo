#!/usr/bin/env python3
"""Manual Phase 3 write-convergence validation using disposable databases only."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4

import app.models  # noqa: F401
from app.db.base import Base
from app.db.memory_v2_migrations import MemoryV2MigrationError
from app.db.session import build_engine
from app.services.memory_v2.adapters import (
    CandidateReviewAction,
    CandidateReviewV2Adapter,
    ChatMemoryV2Adapter,
    GenericMemoryV2Adapter,
    ImportMemoryV2Adapter,
    MemoryV2AdapterContext,
    StructuredMemoryInput,
    TypedMemoryV2Adapter,
)
from app.services.memory_v2.contracts import (
    ActorKind,
    ReplacementAuthority,
    SourceKind,
    TargetRevision,
)
from app.services.memory_v2.coordinator import (
    MemoryV2ExecutionContext,
    MemoryV2MutationCoordinator,
)
from app.services.memory_v2.disposable_crypto import DisposableMemoryCrypto
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.idempotency import MemoryV2Idempotency
from app.services.memory_v2.source_changes import MemoryV2SourceChangeCoordinator
from app.services.memory_v2.taxonomy import Cardinality, MemoryType

OWNER_A = "00000000-0000-4000-8000-000000000001"
OWNER_B = "00000000-0000-4000-8000-000000000002"
OLD_GOAL = "create long-form cinematic YouTube videos"
NEW_GOAL = "create short Instagram reels clearly"
LEGACY_TABLES = (
    "memories",
    "profile_facts",
    "preferences",
    "goals",
    "projects",
    "education",
    "employment",
    "activities",
    "events",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="Retain disposable artifacts.")
    return parser


def _video_goal(value: str) -> StructuredMemoryInput:
    return StructuredMemoryInput(
        memory_type=MemoryType.GOAL,
        domain_key="video_creation",
        slot_key="goal:video_creation:current_primary_goal",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=value,
        display_text=value,
    )


def _harness(root: Path, profile_id: str, owner_id: str = OWNER_A):
    database_path = root / profile_id / "neo.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{database_path}"
    crypto = DisposableMemoryCrypto(seed=b"phase3-manual-disposable-seed-material")
    flags = MemoryV2FeatureFlags(
        schema_enabled=True,
        canonical_writes=True,
        enabled_owner_ids=frozenset({OWNER_A, OWNER_B}),
        disposable_database_root=str(root),
    )
    coordinator = MemoryV2MutationCoordinator(
        flags=flags,
        payload_provider=crypto,
        fingerprint_provider=crypto,
        tombstone_provider=crypto,
        key_versions=crypto,
    )
    execution = MemoryV2ExecutionContext(
        owner_id=owner_id,
        database_identity=f"account-profile:{profile_id}",
        database_url=database_url,
        profile_id=profile_id,
        disposable=True,
    )
    context = MemoryV2AdapterContext(
        execution=execution,
        actor_kind=ActorKind.USER,
        actor_id="manual-phase3-user",
        source_kind=SourceKind.MANUAL_UI,
        source_id="manual-phase3-source",
        request_id="manual-phase3-request",
        session_id="manual-phase3-session",
        conversation_id="manual-phase3-chat",
    )
    return database_path, coordinator, context


def _legacy_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in LEGACY_TABLES
            if table in tables
        }
    finally:
        connection.close()


def _inspect(path: Path) -> dict[str, list[dict[str, object]]]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        queries = {
            "bindings": "SELECT * FROM memory_owner_bindings_v2",
            "records": (
                "SELECT id, owner_id, memory_type, domain_key, slot_key, canonical_payload, "
                "display_text, status, revision FROM memory_records_v2 ORDER BY created_at, id"
            ),
            "relations": "SELECT * FROM memory_relations_v2 ORDER BY created_at, id",
            "sources": "SELECT * FROM memory_sources_v2 ORDER BY created_at, id",
            "operations": (
                "SELECT id, owner_id, idempotency_key, operation_kind, status, outcome, "
                "rejection_code, error_code FROM memory_operations_v2 ORDER BY created_at, id"
            ),
            "tombstones": "SELECT * FROM memory_tombstones_v2 ORDER BY created_at, id",
            "outbox": (
                "SELECT id, owner_id, event_kind, memory_id, canonical_revision, state "
                "FROM memory_outbox_v2 ORDER BY created_at, id"
            ),
        }
        return {
            name: [dict(row) for row in connection.execute(query).fetchall()]
            for name, query in queries.items()
        }
    finally:
        connection.close()


def _print_result(label: str, result) -> None:
    if hasattr(result, "compatibility") and result.compatibility is not None:
        payload = asdict(result.compatibility)
    elif hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    else:
        payload = asdict(result)
    print(f"{label}: {json.dumps(payload, sort_keys=True, default=str)}")


def _replacement_surface(root: Path, surface: str) -> dict[str, object]:
    path, coordinator, context = _harness(root, f"replacement-{surface}")
    generic = GenericMemoryV2Adapter(coordinator)
    old = generic.create(
        context,
        _video_goal(OLD_GOAL),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, f"{surface}-old"),
    )
    assert old.compatibility and old.compatibility.active_memory_id
    target = TargetRevision(
        memory_id=UUID(old.compatibility.active_memory_id),
        expected_revision=1,
    )
    if surface == "generic":
        result = generic.replace(
            context,
            _video_goal(NEW_GOAL),
            (target,),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            idempotency_key=MemoryV2Idempotency.http(OWNER_A, "surface-replace", "replace"),
        )
    elif surface == "review":
        reviewed = CandidateReviewV2Adapter(coordinator).apply(
            context,
            candidate_id="surface-review-candidate",
            candidate_revision=1,
            action=CandidateReviewAction.REPLACE,
            item=_video_goal(NEW_GOAL),
            targets=(target,),
        )
        assert reviewed.coordination is not None
        result = reviewed.coordination
    else:
        chat_context = replace(
            context,
            source_kind=SourceKind.AUTOMATIC_EXTRACTION,
            source_id="surface-chat-source",
            message_id="surface-chat-message",
        )
        result = ChatMemoryV2Adapter(coordinator).apply_structured_replacement(
            chat_context,
            _video_goal(NEW_GOAL),
            (target,),
            extraction_version="legacy-structured-v1",
            candidate_key="surface-replacement",
            transport="sync",
        )
    assert result.compatibility and result.compatibility.outcome == "replaced"
    state = _inspect(path)
    active = [row for row in state["records"] if row["status"] == "active"]
    old_rows = [row for row in state["records"] if row["status"] == "superseded"]
    assert len(active) == 1 and len(old_rows) == 1
    assert active[0]["canonical_payload"] == f'"{NEW_GOAL}"'
    assert active[0]["domain_key"] == "video_creation"
    assert active[0]["slot_key"] == old_rows[0]["slot_key"]
    assert [row["relation_type"] for row in state["relations"]] == ["supersedes"]
    return {
        "surface": surface,
        "database": str(path),
        "outcome": result.compatibility.outcome,
        "active": active[0]["canonical_payload"],
    }


def _run(root: Path) -> None:
    database_path, coordinator, context = _harness(root, "main")
    engine = build_engine(f"sqlite:///{database_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    legacy_before = _legacy_counts(database_path)
    print(f"disposable_database={database_path}")

    generic = GenericMemoryV2Adapter(coordinator)
    typed = TypedMemoryV2Adapter(coordinator)
    review = CandidateReviewV2Adapter(coordinator)
    imported = ImportMemoryV2Adapter(coordinator)

    old = generic.create(
        context,
        _video_goal(OLD_GOAL),
        idempotency_key=MemoryV2Idempotency.http(OWNER_A, "generic-create", "create"),
    )
    _print_result("generic_create", old)
    assert old.compatibility and old.compatibility.active_memory_id

    learning = StructuredMemoryInput(
        memory_type=MemoryType.GOAL,
        domain_key="learning",
        slot_key=f"goal:learning:independent:{uuid4()}",
        cardinality=Cardinality.ADDITIVE,
        canonical_value="learn deterministic video editing",
        display_text="learn deterministic video editing",
    )
    typed_goal = typed.create_typed(context, learning, client_mutation_id="typed-goal")
    _print_result("typed_goal", typed_goal)
    assert typed_goal.compatibility and typed_goal.compatibility.active_memory_id

    replacement = review.apply(
        context,
        candidate_id="manual-review-replacement",
        candidate_revision=1,
        action=CandidateReviewAction.REPLACE,
        item=_video_goal(NEW_GOAL),
        targets=(
            TargetRevision(memory_id=UUID(old.compatibility.active_memory_id), expected_revision=1),
        ),
    )
    _print_result("candidate_review_replacement", replacement)
    assert replacement.coordination and replacement.coordination.compatibility
    replacement_id = replacement.coordination.compatibility.active_memory_id
    assert replacement_id

    chat_context = replace(
        context,
        source_kind=SourceKind.AUTOMATIC_EXTRACTION,
        source_id="chat-structured-source",
        message_id="chat-structured-message",
    )
    chat = ChatMemoryV2Adapter(coordinator)
    sync = chat.apply_structured_candidate(
        chat_context,
        _video_goal(NEW_GOAL),
        extraction_version="legacy-structured-v1",
        candidate_key="candidate-0",
        transport="sync",
    )
    stream = chat.apply_structured_candidate(
        chat_context,
        _video_goal(NEW_GOAL),
        extraction_version="legacy-structured-v1",
        candidate_key="candidate-0",
        transport="stream",
    )
    _print_result("sync_chat", sync)
    _print_result("stream_chat", stream)
    assert sync.mutation and stream.mutation
    assert sync.mutation.operation_id == stream.mutation.operation_id

    forgotten = generic.forget(
        context,
        TargetRevision(
            memory_id=UUID(typed_goal.compatibility.active_memory_id),
            expected_revision=1,
        ),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "explicit-forget"),
    )
    _print_result("explicit_forget", forgotten)
    assert forgotten.compatibility and forgotten.compatibility.outcome == "forgotten"
    restore = generic.restore(
        context,
        TargetRevision(
            memory_id=UUID(typed_goal.compatibility.active_memory_id),
            expected_revision=2,
        ),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "unsafe-restore"),
    )
    _print_result("restore_rejection", restore)
    assert restore.compatibility and restore.compatibility.outcome == "rejected"

    imported_result = imported.accept(
        replace(context, source_kind=SourceKind.IMPORT, source_id="import-batch-1"),
        {
            "owner_id": OWNER_B,
            "id": "foreign-id",
            "status": "active",
            "memory_type": "knowledge",
            "domain_key": "software_development",
            "slot_key": f"knowledge:software_development:item:{uuid4()}",
            "cardinality": "additive",
            "canonical_value": "Neo Phase 3 uses one mutation coordinator",
            "display_text": "Neo Phase 3 uses one mutation coordinator",
        },
        batch_id="import-batch-1",
        item_hash="knowledge-item-1",
    )
    _print_result("import_acceptance", imported_result)

    before_source_change = _inspect(database_path)
    source_memory = next(
        row for row in before_source_change["records"] if row["id"] == replacement_id
    )
    supporting_sources = [
        row
        for row in before_source_change["sources"]
        if row["memory_id"] == replacement_id
        and row["assertion_role"] == "supports"
        and row["is_active"]
    ]
    assert len(supporting_sources) > 1
    detached_source = next(
        row for row in supporting_sources if row["message_id"] == "chat-structured-message"
    )
    operations_before_source_change = len(before_source_change["operations"])
    removal_events_before_source_change = len(
        [row for row in before_source_change["outbox"] if row["event_kind"] == "canonical_remove"]
    )

    source_change = MemoryV2SourceChangeCoordinator(generic).delete_message_source(
        context,
        message_id="chat-structured-message",
        edit_revision=2,
        target=TargetRevision(memory_id=UUID(replacement_id), expected_revision=2),
        source_id=UUID(detached_source["id"]),
    )
    _print_result("source_delete_with_other_support", source_change)
    assert source_change.action.value == "detach_source"
    assert source_change.outcome.value == "preserved"
    assert source_change.review_required is False
    assert source_change.remaining_active_source_count == 1
    assert source_change.canonical_mutation_performed is False
    assert source_change.canonical_revision_changed is False

    after_source_change = _inspect(database_path)
    persisted_detached = next(
        row for row in after_source_change["sources"] if row["id"] == detached_source["id"]
    )
    remaining_supports = [
        row
        for row in after_source_change["sources"]
        if row["memory_id"] == replacement_id
        and row["assertion_role"] == "supports"
        and row["is_active"]
    ]
    persisted_memory = next(
        row for row in after_source_change["records"] if row["id"] == replacement_id
    )
    assert persisted_detached["is_active"] == 0
    assert persisted_detached["detachment_reason"] == "source_deleted"
    assert len(remaining_supports) == 1
    assert persisted_memory["status"] == "active"
    assert persisted_memory["revision"] == source_memory["revision"] == 2
    assert len(after_source_change["operations"]) == operations_before_source_change
    assert len(
        [row for row in after_source_change["outbox"] if row["event_kind"] == "canonical_remove"]
    ) == removal_events_before_source_change
    print(
        "source_delete_with_other_support_proof: "
        + json.dumps(
            {
                "detached_source_id": persisted_detached["id"],
                "detached_source_is_active": bool(persisted_detached["is_active"]),
                "memory_id": persisted_memory["id"],
                "memory_revision_after": persisted_memory["revision"],
                "memory_revision_before": source_memory["revision"],
                "memory_status": persisted_memory["status"],
                "remaining_active_source_count": len(remaining_supports),
                "remaining_active_source_id": remaining_supports[0]["id"],
                "remaining_source_is_active": bool(remaining_supports[0]["is_active"]),
            },
            sort_keys=True,
        )
    )

    before_security = len(_inspect(database_path)["operations"])
    owner_b_context = replace(
        context,
        execution=replace(context.execution, owner_id=OWNER_B),
    )
    try:
        generic.create(
            owner_b_context,
            _video_goal("cross-owner write must fail"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_B, "cross-owner"),
        )
    except MemoryV2MigrationError as exc:
        print(f"cross_owner_rejection={exc}")
    else:
        raise AssertionError("cross-owner mutation unexpectedly succeeded")

    incognito = generic.create(
        replace(context, execution=replace(context.execution, is_incognito=True)),
        _video_goal("incognito must not write"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "incognito"),
    )
    _print_result("incognito", incognito)
    assert not incognito.v2_called
    assert len(_inspect(database_path)["operations"]) == before_security

    disabled_path, disabled_coordinator, disabled_context = _harness(root, "disabled")
    disabled_coordinator.flags = MemoryV2FeatureFlags()
    disabled = GenericMemoryV2Adapter(disabled_coordinator).create(
        disabled_context,
        _video_goal("disabled path remains legacy"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "disabled"),
    )
    print(f"feature_disabled={disabled.mode.value}:{disabled.reason}")
    assert not disabled.v2_called and disabled.legacy_write_allowed
    assert not disabled_path.exists()

    retry = imported.accept(
        replace(context, source_kind=SourceKind.IMPORT, source_id="import-batch-1"),
        {
            "memory_type": "knowledge",
            "domain_key": "software_development",
            "slot_key": next(
                row["slot_key"]
                for row in _inspect(database_path)["records"]
                if row["memory_type"] == "knowledge"
            ),
            "cardinality": "additive",
            "canonical_value": "Neo Phase 3 uses one mutation coordinator",
            "display_text": "Neo Phase 3 uses one mutation coordinator",
        },
        batch_id="import-batch-1",
        item_hash="knowledge-item-1",
    )
    assert imported_result.mutation and retry.mutation
    assert imported_result.mutation.operation_id == retry.mutation.operation_id
    _print_result("idempotent_retry", retry)

    surface_results = [
        _replacement_surface(root, surface) for surface in ("generic", "review", "chat")
    ]
    print(f"replacement_surface_parity={json.dumps(surface_results, sort_keys=True)}")

    state = _inspect(database_path)
    legacy_after = _legacy_counts(database_path)
    assert legacy_before == legacy_after
    active_video = [
        row
        for row in state["records"]
        if row["status"] == "active"
        and row["slot_key"] == "goal:video_creation:current_primary_goal"
    ]
    superseded_video = [
        row
        for row in state["records"]
        if row["status"] == "superseded"
        and row["slot_key"] == "goal:video_creation:current_primary_goal"
    ]
    assert len(active_video) == 1 and len(superseded_video) == 1
    assert active_video[0]["canonical_payload"] == f'"{NEW_GOAL}"'
    assert active_video[0]["display_text"] == NEW_GOAL
    assert state["relations"]
    assert all(row["owner_id"] == OWNER_A for row in state["records"])
    print(f"legacy_counts_unchanged={json.dumps(legacy_after, sort_keys=True)}")
    for table, rows in state.items():
        print(f"{table}={json.dumps(rows, sort_keys=True, default=str)}")
    print("phase3_manual_validation=PASS")


def main() -> int:
    args = _parser().parse_args()
    root = Path(tempfile.mkdtemp(prefix="neo-memory-v2-phase3-"))
    print(f"disposable_root={root}")
    try:
        _run(root)
    except Exception as exc:
        print(f"phase3_manual_validation=FAIL:{type(exc).__name__}:{exc}")
        print(f"artifacts_retained={root}")
        return 1
    if args.keep:
        print(f"artifacts_retained={root}")
        print(f"cleanup_command=rm -rf -- {root}")
    else:
        shutil.rmtree(root)
        print("artifacts_cleaned=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
