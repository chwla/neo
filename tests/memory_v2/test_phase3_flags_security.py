from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from app.core.config import Settings
from app.services.memory_v2.adapters import GenericMemoryV2Adapter, MemoryV2AdapterError
from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.coordinator import MemoryV2CoordinationError
from app.services.memory_v2.feature_flags import (
    MemoryV2FeatureFlags,
    MemoryV2RolloutError,
    MemoryV2WriteMode,
)
from app.services.memory_v2.idempotency import MemoryV2Idempotency
from tests.memory_v2.phase3_helpers import OWNER_A, OWNER_B, phase3_harness, video_goal


def test_production_feature_flag_defaults_are_legacy() -> None:
    settings = Settings(_env_file=None)
    assert not settings.memory_v2_schema_enabled
    assert not settings.memory_v2_shadow_mutations
    assert not settings.memory_v2_canonical_writes
    assert settings.memory_v2_legacy_compatibility
    assert settings.memory_v2_enabled_owner_ids == ""
    assert settings.memory_v2_disposable_database_root == ""


@pytest.mark.parametrize(
    "values",
    [
        {"shadow_mutations": True},
        {"canonical_writes": True},
        {"schema_enabled": True, "shadow_mutations": True, "canonical_writes": True},
        {"schema_enabled": True, "canonical_writes": True},
        {
            "schema_enabled": True,
            "canonical_writes": True,
            "enabled_owner_ids": frozenset({OWNER_A}),
        },
    ],
)
def test_invalid_or_dangerous_flag_combinations_fail_closed(values) -> None:
    with pytest.raises(MemoryV2RolloutError):
        MemoryV2FeatureFlags(**values)


def test_owner_allowlist_prevents_fleet_wide_enablement(tmp_path) -> None:
    harness = phase3_harness(tmp_path, enabled_owners=frozenset({OWNER_A}))
    flags = harness.coordinator.flags
    assert flags.mode_for(OWNER_A) is MemoryV2WriteMode.CANONICAL
    assert flags.mode_for(OWNER_B) is MemoryV2WriteMode.SCHEMA_ONLY


def test_schema_only_disabled_owner_makes_no_v2_call(tmp_path) -> None:
    harness = phase3_harness(
        tmp_path,
        owner_id=OWNER_B,
        enabled_owners=frozenset({OWNER_A}),
    )
    result = GenericMemoryV2Adapter(harness.coordinator).create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_B, "disabled-owner"),
    )
    assert result.mode is MemoryV2WriteMode.SCHEMA_ONLY
    assert not result.v2_called
    assert result.legacy_write_allowed
    assert not harness.database_path.exists()


def test_isolated_shadow_validation_calls_kernel_without_canonical_rows(tmp_path) -> None:
    harness = phase3_harness(tmp_path, canonical=False, shadow=True)
    result = GenericMemoryV2Adapter(harness.coordinator).create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "shadow"),
    )
    assert result.mode is MemoryV2WriteMode.SHADOW
    assert result.v2_called
    assert not result.legacy_write_allowed
    assert result.compatibility is not None
    assert not result.compatibility.committed

    connection = sqlite3.connect(harness.database_path)
    try:
        assert connection.execute("SELECT count(*) FROM memory_operations_v2").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM memory_records_v2").fetchone()[0] == 0
    finally:
        connection.close()


def test_compatibility_mapping_can_be_explicitly_disabled(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    harness.coordinator.flags = replace(
        harness.coordinator.flags,
        legacy_compatibility=False,
    )
    result = GenericMemoryV2Adapter(harness.coordinator).create(
        harness.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "no-compatibility"),
    )
    assert result.mutation is not None
    assert result.compatibility is None


def test_incognito_and_memory_disabled_make_zero_v2_calls(tmp_path) -> None:
    for name, context_changes, rejection in (
        ("incognito", {"is_incognito": True}, "incognito_disabled"),
        ("disabled", {"memory_enabled": False}, "memory_disabled"),
    ):
        harness = phase3_harness(tmp_path / name)
        context = replace(
            harness.context,
            execution=replace(harness.context.execution, **context_changes),
        )
        result = GenericMemoryV2Adapter(harness.coordinator).create(
            context,
            video_goal("create short Instagram reels clearly"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_A, name),
        )
        assert not result.v2_called
        assert not result.legacy_write_allowed
        assert result.compatibility is not None
        assert result.compatibility.rejection_code == rejection
        assert not harness.database_path.exists()


def test_missing_invalid_or_mismatched_owner_context_fails_closed(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    key = MemoryV2Idempotency.manual(OWNER_A, "owner-check")
    command_context = harness.context
    for execution in (
        replace(command_context.execution, owner_id=""),
        replace(command_context.execution, database_identity=""),
        replace(command_context.execution, profile_id="other-profile"),
        replace(command_context.execution, database_identity="guest-profile:disposable-one"),
    ):
        with pytest.raises((ValueError, MemoryV2CoordinationError)):
            adapter.create(
                replace(command_context, execution=execution),
                video_goal("create short Instagram reels clearly"),
                idempotency_key=key,
            )


def test_default_database_fallback_and_non_disposable_paths_cannot_reach_v2(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    key = MemoryV2Idempotency.manual(OWNER_A, "path-check")
    invalid_contexts = (
        replace(harness.context.execution, database_url=""),
        replace(harness.context.execution, disposable=False),
        replace(
            harness.context.execution,
            database_url=f"sqlite:///{tmp_path / 'outside.db'}",
        ),
    )
    for execution in invalid_contexts:
        with pytest.raises((MemoryV2CoordinationError, MemoryV2RolloutError)):
            adapter.create(
                replace(harness.context, execution=execution),
                video_goal("create short Instagram reels clearly"),
                idempotency_key=key,
            )


def test_guest_uses_an_ephemeral_guest_binding_and_cannot_cross_to_permanent(tmp_path) -> None:
    guest = phase3_harness(
        tmp_path,
        profile_id="guest-session",
        guest=True,
    )
    adapter = GenericMemoryV2Adapter(guest.coordinator)
    created = adapter.create(
        guest.context,
        video_goal("create short Instagram reels clearly"),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "guest-create"),
    )
    assert created.compatibility and created.compatibility.outcome == "created"

    permanent_context = replace(
        guest.context,
        execution=replace(guest.context.execution, is_guest=False),
    )
    with pytest.raises(
        MemoryV2CoordinationError,
        match="guest_permanent_database_binding_mismatch",
    ):
        adapter.create(
            permanent_context,
            video_goal("must not cross guest boundary"),
            idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "guest-cross"),
        )


def test_prohibited_and_sensitive_adapter_failures_do_not_echo_content(tmp_path) -> None:
    harness = phase3_harness(tmp_path)
    adapter = GenericMemoryV2Adapter(harness.coordinator)
    prohibited_text = "password is [redacted]"
    prohibited = adapter.create(
        harness.context,
        video_goal(prohibited_text),
        idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "prohibited"),
    )
    assert prohibited.compatibility is not None
    rendered = repr(prohibited.compatibility)
    assert prohibited_text not in rendered
    assert prohibited.compatibility.outcome == "rejected"

    sensitive = replace(
        video_goal("private health preference"),
        sensitivity=Sensitivity.SENSITIVE,
    )
    with pytest.raises(MemoryV2AdapterError) as failure:
        adapter.create(
            harness.context,
            sensitive,
            idempotency_key=MemoryV2Idempotency.manual(OWNER_A, "sensitive"),
        )
    assert "private health preference" not in str(failure.value)
