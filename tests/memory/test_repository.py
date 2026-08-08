from __future__ import annotations

import inspect as python_inspect
from datetime import UTC, datetime

import pytest

from app.models.memory import MemoryRelation, MemorySource
from app.repositories.memory import (
    MemoryBindingError,
    MemoryNotFoundError,
    MemoryProhibitedContentError,
    MemoryRepository,
    MemoryRevisionConflict,
)
from app.services.memory.contracts import MemoryLifecycleState, SourceKind
from tests.memory.factories import (
    DATABASE_IDENTITY,
    OWNER_A,
    OWNER_B,
    operation,
    record,
    uuid_string,
)


def _repository(session, *, owner_id: str = OWNER_A) -> MemoryRepository:
    return MemoryRepository(
        session,
        owner_id=owner_id,
        database_identity=DATABASE_IDENTITY,
    )


def test_owner_and_database_identity_are_mandatory(memory_session) -> None:
    with pytest.raises(TypeError):
        MemoryRepository(memory_session)  # type: ignore[call-arg]
    with pytest.raises((TypeError, ValueError)):
        MemoryRepository(
            memory_session,
            owner_id=None,  # type: ignore[arg-type]
            database_identity=DATABASE_IDENTITY,
        )
    with pytest.raises(MemoryBindingError):
        MemoryRepository(
            memory_session,
            owner_id=OWNER_A,
            database_identity="",
        )


def test_wrong_owner_database_pair_fails_closed(memory_session) -> None:
    with pytest.raises(MemoryBindingError):
        _repository(memory_session, owner_id=OWNER_B)


def test_reads_are_owner_and_status_explicit(memory_session) -> None:
    memory_session.add_all(
        [
            operation(owner_id=OWNER_A, number=100),
            operation(owner_id=OWNER_B, number=101),
            record(owner_id=OWNER_A, number=200),
            record(
                owner_id=OWNER_B,
                number=201,
                operation_id=uuid_string(101),
                slot_key="goal:learning:current_primary_goal",
            ),
        ]
    )
    memory_session.flush()
    repository = _repository(memory_session)

    records = repository.list_records(statuses=[MemoryLifecycleState.ACTIVE])
    assert [item.id for item in records] == [uuid_string(200)]
    assert repository.get_record(uuid_string(201), statuses=[MemoryLifecycleState.ACTIVE]) is None
    with pytest.raises(ValueError, match="explicit_status_filter_required"):
        repository.list_records(statuses=[])


def test_cross_owner_mutations_are_rejected_before_insert(memory_session) -> None:
    repository = _repository(memory_session)
    with pytest.raises(MemoryNotFoundError):
        repository.add_operation(operation(owner_id=OWNER_B))

    source = MemorySource(
        id=uuid_string(300),
        owner_id=OWNER_B,
        memory_id=uuid_string(201),
        source_kind=SourceKind.CHAT_MESSAGE.value,
        source_content_hash="source-hash",
        observed_at=datetime.now(UTC),
        assertion_role="supports",
        is_active=True,
        operation_id=uuid_string(101),
        schema_version=1,
    )
    relation = MemoryRelation(
        id=uuid_string(400),
        owner_id=OWNER_B,
        from_memory_id=uuid_string(201),
        relation_type="refines",
        to_memory_id=uuid_string(202),
        operation_id=uuid_string(101),
        schema_version=1,
    )
    with pytest.raises(MemoryNotFoundError):
        repository.add_source(source)
    with pytest.raises(MemoryNotFoundError):
        repository.add_relation(relation)


def test_expected_revision_update_is_atomic(memory_session) -> None:
    repository = _repository(memory_session)
    repository.add_operation(operation(number=100))
    repository.add_record(record(number=200))

    updated = repository.update_record_fields(
        uuid_string(200), expected_revision=1, values={"importance": 8}
    )
    assert updated.importance == 8
    assert updated.revision == 2
    with pytest.raises(MemoryRevisionConflict):
        repository.update_record_fields(
            uuid_string(200), expected_revision=1, values={"importance": 9}
        )


def test_repository_does_not_commit_caller_transaction(memory_session, monkeypatch) -> None:
    def unexpected_commit() -> None:
        raise AssertionError("repository committed the caller-owned transaction")

    monkeypatch.setattr(memory_session, "commit", unexpected_commit)
    repository = _repository(memory_session)
    repository.add_operation(operation(number=100))
    repository.add_record(record(number=200))


def test_repository_rejects_prohibited_material_without_echoing_it(memory_session) -> None:
    repository = _repository(memory_session)
    item = operation(number=100)
    item.normalized_command_json = {"note": "password is [redacted]"}
    with pytest.raises(
        MemoryProhibitedContentError,
        match="^prohibited_content_not_persisted$",
    ):
        repository.add_operation(item)


def test_repository_has_only_persistence_primitives_and_forbidden_dependencies() -> None:
    public_methods = {
        name
        for name, value in vars(MemoryRepository).items()
        if callable(value) and not name.startswith("_")
    }
    forbidden_lifecycle = {
        "replace",
        "restore",
        "resurrect",
        "merge",
        "decide_duplicate",
        "apply_correction",
    }
    assert public_methods.isdisjoint(forbidden_lifecycle)

    source = python_inspect.getsource(python_inspect.getmodule(MemoryRepository))
    for forbidden in ("embedding", "vector", "fts", "httpx", "requests", "background"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source
    assert ".commit(" not in source
