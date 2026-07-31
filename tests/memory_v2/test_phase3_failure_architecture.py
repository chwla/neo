from __future__ import annotations

import ast
import sqlite3
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

import app.services.memory_v2.coordinator as coordinator_module
from app.db.memory_v2_migrations import MemoryV2MigrationError
from app.services.memory_v2.adapters import GenericMemoryV2Adapter
from app.services.memory_v2.compatibility import map_compatibility_result
from app.services.memory_v2.contracts import (
    MemoryCommandResult,
    MemoryErrorCode,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
)
from app.services.memory_v2.idempotency import MemoryV2Idempotency
from tests.memory_v2.phase3_helpers import OWNER_A, OWNER_B, phase3_harness, video_goal

PHASE3_RUNTIME_FILES = (
    "app/services/memory_v2/adapters.py",
    "app/services/memory_v2/compatibility.py",
    "app/services/memory_v2/coordinator.py",
    "app/services/memory_v2/disposable_crypto.py",
    "app/services/memory_v2/feature_flags.py",
    "app/services/memory_v2/idempotency.py",
    "app/services/memory_v2/source_changes.py",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _row_count(path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def test_phase3_architecture_has_one_kernel_boundary_and_no_forbidden_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative in PHASE3_RUNTIME_FILES:
        path = root / relative
        source = path.read_text(encoding="utf-8").casefold()
        imports = _imports(path)
        assert "app.models.memory_v2" not in imports
        assert "app.repositories.memory_v2" not in imports
        for forbidden in ("embedding", "vector", "qdrant", "fts", "httpx", "requests"):
            assert forbidden not in source
        if relative.endswith("coordinator.py"):
            assert "memorymutationservice" in source
            assert ".execute(" in source
        else:
            assert "memorymutationservice" not in source


def test_compatibility_mapping_preserves_every_typed_outcome_and_code() -> None:
    rejection_outcomes = {
        MemoryOutcome.NEEDS_REVIEW,
        MemoryOutcome.REJECTED,
        MemoryOutcome.DISABLED,
    }
    for outcome in MemoryOutcome:
        result = MemoryCommandResult(
            operation_id=UUID("00000000-0000-4000-8000-000000000900"),
            owner_id=OWNER_A,
            operation=MemoryOperationKind.CREATE,
            outcome=outcome,
            rejection_code=(
                MemoryRejectionCode.AMBIGUOUS_CONFLICT if outcome in rejection_outcomes else None
            ),
            error_code=(
                MemoryErrorCode.INTERNAL_ERROR if outcome is MemoryOutcome.FAILED else None
            ),
        )
        mapped = map_compatibility_result(result)
        assert mapped.outcome == outcome.value
        assert mapped.operation_id == str(result.operation_id)
        assert mapped.review_required is (outcome is MemoryOutcome.NEEDS_REVIEW)
        assert (mapped.rejection_code is not None) is (outcome in rejection_outcomes)
        assert (mapped.error_code is not None) is (outcome is MemoryOutcome.FAILED)


def test_legacy_runtime_writers_have_no_partial_v2_import_or_cutover() -> None:
    root = Path(__file__).resolve().parents[2]
    legacy_writers = (
        "app/api/routes/memory.py",
        "app/services/chat.py",
        "app/services/review.py",
        "app/services/extraction.py",
        "app/services/lifecycle.py",
        "app/services/lifecycle_maintenance.py",
        "app/services/reflection.py",
        "app/repositories/memory_store.py",
    )
    for relative in legacy_writers:
        source = (root / relative).read_text(encoding="utf-8")
        assert "app.services.memory_v2" not in source
        assert "app.models.memory_v2" not in source


def test_service_construction_and_execution_failures_do_not_fallback_or_partially_write(
    tmp_path,
) -> None:
    harness = phase3_harness(tmp_path / "construction")

    def construction_failure(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("construction_failed")

    harness.coordinator._service_factory = construction_failure
    with pytest.raises(RuntimeError, match="construction_failed"):
        GenericMemoryV2Adapter(harness.coordinator).create(
            harness.context,
            video_goal("create short Instagram reels clearly"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "construction-failure"),
        )
    assert _row_count(harness.database_path, "memory_operations_v2") == 0
    assert _row_count(harness.database_path, "memory_records_v2") == 0

    execution = phase3_harness(tmp_path / "execution")

    class FailingService:
        def execute(self, command):
            del command
            raise RuntimeError("execution_failed")

    def execution_failure(*args, **kwargs):
        del args, kwargs
        return FailingService()

    execution.coordinator._service_factory = execution_failure
    with pytest.raises(RuntimeError, match="execution_failed"):
        GenericMemoryV2Adapter(execution.coordinator).create(
            execution.context,
            video_goal("create short Instagram reels clearly"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "execution-failure"),
        )
    assert _row_count(execution.database_path, "memory_operations_v2") == 0
    assert _row_count(execution.database_path, "memory_records_v2") == 0


def test_compatibility_mapping_failure_does_not_rollback_committed_canonical_state(
    tmp_path,
    monkeypatch,
) -> None:
    harness = phase3_harness(tmp_path)
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    key = MemoryV2Idempotency.manual(OWNER_A, "mapping-failure")
    original_mapper = coordinator_module.map_compatibility_result

    def mapping_failure(result):
        del result
        raise RuntimeError("compatibility_mapping_failed")

    monkeypatch.setattr(coordinator_module, "map_compatibility_result", mapping_failure)
    with pytest.raises(RuntimeError, match="compatibility_mapping_failed"):
        adapter.create(
            harness.context,
            video_goal("create short Instagram reels clearly"),
            idempotency_key=key,
        )
    assert _row_count(harness.database_path, "memory_operations_v2") == 1
    assert _row_count(harness.database_path, "memory_records_v2") == 1

    monkeypatch.setattr(coordinator_module, "map_compatibility_result", original_mapper)
    replay = adapter.create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=key,
    )
    assert replay.compatibility and replay.compatibility.outcome == "created"
    assert _row_count(harness.database_path, "memory_operations_v2") == 1


def test_owner_database_mismatch_and_cross_owner_reuse_fail_closed(tmp_path) -> None:
    harness = phase3_harness(
        tmp_path,
        enabled_owners=frozenset({OWNER_A, OWNER_B}),
    )
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    adapter.create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "binding-owner-a"),
    )
    owner_b_context = replace(
        harness.context,
        execution=replace(harness.context.execution, owner_id=OWNER_B),
    )
    with pytest.raises(MemoryV2MigrationError, match="owner_database_binding_mismatch"):
        adapter.create(
            owner_b_context,
            video_goal("create short Instagram reels clearly"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_B, "binding-owner-b"),
        )
    assert _row_count(harness.database_path, "memory_operations_v2") == 1


def test_same_key_conflict_and_owner_scoped_independence(tmp_path) -> None:
    harness = phase3_harness(tmp_path / "owner-a")
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    key = MemoryV2Idempotency.manual(OWNER_A, "same-external-key")
    first = adapter.create(
        harness.context,
        video_goal("create tutorial videos"),
        idempotency_key=key,
    )
    conflict = adapter.create(
        harness.context,
        video_goal("create travel videos"),
        idempotency_key=key,
    )
    assert first.compatibility and first.compatibility.outcome == "created"
    assert conflict.compatibility and conflict.compatibility.error_code == "idempotency_conflict"

    owner_b = phase3_harness(
        tmp_path / "owner-b",
        owner_id=OWNER_B,
        profile_id="owner-b-profile",
    )
    second = GenericMemoryV2Adapter(owner_b.coordinator).create(
        owner_b.context,
        video_goal("create travel videos"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_B, "same-external-key"),
    )
    assert second.compatibility and second.compatibility.outcome == "created"
