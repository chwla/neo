from __future__ import annotations

import inspect
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.memory_v2 import MemoryOperationV2, MemoryRecordV2
from app.services.memory_v2.contracts import (
    CreateMemoryCommand,
    MemoryErrorCode,
    MemoryOutcome,
    Sensitivity,
)
from app.services.memory_v2.crypto import build_associated_data
from app.services.memory_v2.normalization import (
    canonical_fingerprint,
    canonical_json_bytes,
    normalize_candidate,
    operation_request_hash,
)
from app.services.memory_v2.planner import PlannerContext, PlannerState, plan_memory_mutation
from app.services.memory_v2.taxonomy import MemoryType
from tests.memory_v2.helpers import OWNER_A, OWNER_B, actor, candidate, source


def test_normalization_and_request_hash_are_deterministic(test_crypto) -> None:
    proposal = candidate(
        {"format": " concise ", "examples": True},
        display="Use   concise answers",
        memory_type=MemoryType.PREFERENCE,
        domain="software_development",
        slot="preference:software_development:answer_format",
    )
    first = normalize_candidate(
        proposal,
        owner_id=OWNER_A,
        keyed_provider=test_crypto,
    )
    second = normalize_candidate(
        proposal,
        owner_id=OWNER_A,
        keyed_provider=test_crypto,
    )
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="normalization-hash",
        actor=actor(),
        source=source(),
        candidate=proposal,
    )
    assert first == second
    assert first.canonical_value == {"examples": True, "format": "concise"}
    assert first.display_text == "Use concise answers"
    assert operation_request_hash(
        command,
        keyed_provider=test_crypto,
        sensitivity=Sensitivity.NORMAL,
    ) == operation_request_hash(
        command,
        keyed_provider=test_crypto,
        sensitivity=Sensitivity.NORMAL,
    )
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


def test_sensitive_fingerprint_is_keyed_and_owner_scoped(test_crypto) -> None:
    arguments = {
        "subject_key": "user",
        "memory_type": MemoryType.GOAL,
        "domain_key": "video_creation",
        "slot_key": "goal:video_creation:current_primary_goal",
        "canonical_value": "private preference",
        "sensitivity": Sensitivity.SENSITIVE,
        "keyed_provider": test_crypto,
    }
    first = canonical_fingerprint(owner_id=OWNER_A, **arguments)
    second = canonical_fingerprint(owner_id=OWNER_B, **arguments)
    assert first.startswith("keyed:test-fingerprint-v1:")
    assert first != second
    assert "private" not in first


def test_aead_associated_data_binds_every_required_identity_field() -> None:
    record_id = str(uuid4())
    aad = build_associated_data(
        owner_id=OWNER_A,
        memory_type="goal",
        domain_key="video_creation",
        slot_key="goal:video_creation:current_primary_goal",
        record_id=record_id,
        schema_version=1,
        key_version="test-encryption-v1",
        purpose="canonical_record",
    ).decode()
    for required in (
        OWNER_A,
        "goal",
        "video_creation",
        "goal:video_creation:current_primary_goal",
        record_id,
        "test-encryption-v1",
        '"schema_version":1',
    ):
        assert required in aad


def test_planner_is_pure_and_deterministic(test_crypto) -> None:
    proposal = candidate("create tutorial videos")
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="pure-planner",
        actor=actor(),
        source=source(),
        candidate=proposal,
    )
    normalized = normalize_candidate(
        proposal,
        owner_id=OWNER_A,
        keyed_provider=test_crypto,
    )
    state = PlannerState(owner_id=OWNER_A, records=())
    context = PlannerContext(
        owner_id=OWNER_A,
        operation_id="00000000-0000-4000-8000-000000000900",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        source=__import__(
            "app.services.memory_v2.normalization",
            fromlist=["normalize_source"],
        ).normalize_source(command.source),
        normalized_candidate=normalized,
    )
    assert plan_memory_mutation(command, state, context) == plan_memory_mutation(
        command, state, context
    )


def test_positive_canonical_value_rejects_negated_predecessor_clause(
    mutation_service,
    phase2_engine,
) -> None:
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="negated-canonical",
        actor=actor(),
        source=source(),
        candidate=candidate("I no longer want to create tutorial videos"),
    )
    result = mutation_service.execute(command)
    assert result.outcome is MemoryOutcome.FAILED
    assert result.error_code is MemoryErrorCode.INVALID_COMMAND
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 0
        assert session.scalar(select(func.count(MemoryOperationV2.id))) == 0


def test_dry_run_returns_plan_result_without_writes(mutation_service, phase2_engine) -> None:
    command = CreateMemoryCommand(
        owner_id=OWNER_A,
        idempotency_key="dry-run",
        actor=actor(),
        source=source(),
        candidate=candidate("create tutorial videos"),
        dry_run=True,
    )
    result = mutation_service.execute(command)
    assert result.outcome is MemoryOutcome.CREATED
    with Session(phase2_engine) as session:
        assert session.scalar(select(func.count(MemoryRecordV2.id))) == 0
        assert session.scalar(select(func.count(MemoryOperationV2.id))) == 0


def test_kernel_has_no_derived_or_external_service_dependency() -> None:
    import app.services.memory_v2.mutations as mutations
    import app.services.memory_v2.planner as planner

    source_text = inspect.getsource(mutations) + inspect.getsource(planner)
    forbidden_imports = (
        "import requests",
        "import httpx",
        "from qdrant",
        "import qdrant",
        "embedding",
        "vector_store",
        "background_tasks",
        "fts5",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source_text.casefold()
