from __future__ import annotations

import inspect
import runpy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

import app.api.routes.memory_v2_derived as derived_routes
from app.core.config import Settings
from app.db.memory_v2_migrations import MEMORY_V2_CURRENT_REVISION
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags, MemoryV2RolloutError
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_canonical_mutation_graph_has_no_provider_or_index_dependency() -> None:
    for relative in (
        "app/services/memory_v2/mutations.py",
        "app/services/memory_v2/planner.py",
        "app/services/memory_v2/adapters.py",
        "app/services/memory_v2/coordinator.py",
    ):
        source = _source(relative)
        assert "app.services.embeddings" not in source
        assert "app.services.memory_v2.indexes" not in source
        assert "MemoryV2OutboxProcessor" not in source
        assert ".embed(" not in source
        assert "vector_index" not in source


def test_worker_lease_transaction_contains_no_provider_or_index_call() -> None:
    lease_source = inspect.getsource(MemoryV2OutboxProcessor.lease_batch)
    process_source = inspect.getsource(MemoryV2OutboxProcessor._process_target)
    assert ".embed(" not in lease_source
    assert "fts_index.upsert" not in lease_source
    assert "vector_index.upsert" not in lease_source
    assert ".embed(" in process_source
    assert "fts_index.upsert" in process_source
    assert "vector_index.upsert" in process_source


def test_derived_worker_and_maintenance_cannot_update_canonical_records() -> None:
    source = _source("app/services/memory_v2/outbox.py") + _source(
        "app/services/memory_v2/maintenance.py"
    )
    assert "update(MemoryRecordV2" not in source
    assert "delete(MemoryRecordV2" not in source
    assert "add_record(" not in source
    assert "archive_record(" not in source


def test_phase6_and_legacy_production_defaults_remain_disabled_or_unchanged() -> None:
    fields = Settings.model_fields
    for name in (
        "memory_v2_outbox_worker_enabled",
        "memory_v2_fts_index_enabled",
        "memory_v2_vector_index_enabled",
        "memory_v2_semantic_recall_enabled",
        "memory_v2_reconciliation_enabled",
        "memory_v2_derived_health_routes_enabled",
    ):
        assert fields[name].default is False
    assert fields["memory_v2_legacy_compatibility"].default is True
    assert fields["memory_v2_legacy_read_compatibility"].default is True
    assert fields["semantic_retrieval_enabled"].default is False
    assert fields["auto_embed_memories"].default is False


def test_reconciliation_and_health_flags_require_complete_derived_dependencies() -> None:
    base = {
        "schema_enabled": True,
        "canonical_query_enabled": True,
        "enabled_owner_ids": frozenset({"00000000-0000-4000-8000-000000000001"}),
        "outbox_worker_enabled": True,
    }
    with pytest.raises(MemoryV2RolloutError, match="requires_derived_indexes"):
        MemoryV2FeatureFlags(**base, reconciliation_enabled=True)
    with pytest.raises(MemoryV2RolloutError, match="health_routes_require_reconciliation"):
        MemoryV2FeatureFlags(**base, derived_health_routes_enabled=True)


def test_existing_outbox_is_extended_and_no_phase7_surface_is_added() -> None:
    outbox_source = _source("app/services/memory_v2/outbox.py")
    assert "MemoryOutboxV2" in outbox_source
    assert "MemoryOutboxDeliveryV2" in outbox_source
    assert MEMORY_V2_CURRENT_REVISION == "0002_memory_v2_phase6_derived_indexes"
    assert not [
        path
        for path in (ROOT / "app/services/memory_v2").iterdir()
        if "phase7" in path.name.casefold()
    ]


def test_derived_operational_routes_require_authenticated_enabled_owner(
    monkeypatch,
) -> None:
    owner_id = "00000000-0000-4000-8000-000000000001"
    enabled = Settings(
        memory_v2_schema_enabled=True,
        memory_v2_canonical_query_enabled=True,
        memory_v2_enabled_owner_ids=owner_id,
        memory_v2_outbox_worker_enabled=True,
        memory_v2_fts_index_enabled=True,
        memory_v2_vector_index_enabled=True,
        memory_v2_reconciliation_enabled=True,
        memory_v2_derived_health_routes_enabled=True,
    )
    monkeypatch.setattr(derived_routes, "get_settings", lambda: enabled)
    monkeypatch.setattr(derived_routes, "session_for", lambda _request: None)
    with pytest.raises(HTTPException) as unauthorized:
        derived_routes._authorized_profile(object())
    assert unauthorized.value.status_code == 401

    profile = {"id": "profile-a", "owner_id": owner_id, "is_guest": False}
    monkeypatch.setattr(derived_routes, "session_for", lambda _request: profile)
    authorized, _settings, flags = derived_routes._authorized_profile(object())
    assert authorized == profile
    assert flags.owner_is_enabled(owner_id)

    outside = {**profile, "owner_id": "00000000-0000-4000-8000-000000000099"}
    monkeypatch.setattr(derived_routes, "session_for", lambda _request: outside)
    with pytest.raises(HTTPException) as forbidden:
        derived_routes._authorized_profile(object())
    assert forbidden.value.status_code == 404


def test_reconciliation_route_checkpoint_contract_rejects_malformed_uuid() -> None:
    with pytest.raises(ValueError):
        derived_routes.ReconciliationControl(checkpoint="-" * 36)
    valid = derived_routes.ReconciliationControl(
        checkpoint=(
            "v1:00000000-0000-4000-8000-000000000001:!:00000000-0000-4000-8000-000000000002"
        )
    )
    assert valid.checkpoint.startswith("v1:")


def test_derived_operational_route_source_cannot_mutate_canonical_records() -> None:
    source = _source("app/api/routes/memory_v2_derived.py")
    assert "MemoryRecordV2" not in source
    assert "update(MemoryRecordV2" not in source
    assert "delete(MemoryRecordV2" not in source
    assert 'prefix="/memory-v2/derived"' in source


def test_worker_rejects_request_supplied_owner_without_disposable_authorization() -> None:
    worker = runpy.run_path(str(ROOT / "scripts/memory_v2_index_worker.py"))
    args = SimpleNamespace(
        owner_id=str(uuid4()),
        database_url=None,
        database_identity=None,
        disposable_maintenance=False,
    )
    flags = SimpleNamespace(outbox_worker_enabled=True)
    with pytest.raises(RuntimeError, match="requires_disposable_maintenance"):
        worker["_binding"](args, None, flags)


@pytest.mark.parametrize(
    ("argument", "value"),
    (
        ("--batch-size", "0"),
        ("--lease-seconds", "-1"),
        ("--max-attempts", "0"),
        ("--poll-interval", "0"),
        ("--worker-id", "   "),
    ),
)
def test_worker_rejects_unbounded_or_nonpositive_operational_values(argument, value) -> None:
    worker = runpy.run_path(str(ROOT / "scripts/memory_v2_index_worker.py"))
    with pytest.raises(SystemExit):
        worker["_parser"]().parse_args([argument, value])
