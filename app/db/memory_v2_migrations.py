"""Explicit migration ledger for the Phase 1 memory-v2 schema.

The project does not currently depend on Alembic. This small equivalent ledger is
deliberately scoped to memory v2: ordered immutable revisions, checksums, atomic
upgrade/downgrade functions, unknown-schema refusal, and no opportunistic repair.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.identifiers import canonical_uuid
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryLegacyMapV2,
    MemoryMigrationRunV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryOwnerBindingV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
)

MEMORY_V2_REVISION_0001 = "0001_memory_v2_phase1"
MEMORY_V2_CURRENT_REVISION = MEMORY_V2_REVISION_0001
MEMORY_V2_LEDGER_TABLE = "memory_schema_migrations_v2"

_ledger_metadata = MetaData()
_ledger = Table(
    MEMORY_V2_LEDGER_TABLE,
    _ledger_metadata,
    Column("revision", String(80), primary_key=True),
    Column("revision_checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

_upgrade_tables = (
    MemoryOwnerBindingV2.__table__,
    MemoryOperationV2.__table__,
    MemoryMigrationRunV2.__table__,
    MemoryRecordV2.__table__,
    MemoryCandidateV2.__table__,
    MemorySourceV2.__table__,
    MemoryRelationV2.__table__,
    MemoryOutboxV2.__table__,
    MemoryTombstoneV2.__table__,
    MemoryLegacyMapV2.__table__,
)
MEMORY_V2_TABLES = tuple(table.name for table in _upgrade_tables)


class MemoryV2MigrationError(RuntimeError):
    """Raised when a v2 schema cannot be upgraded or downgraded safely."""


@dataclass(frozen=True)
class MemoryV2MigrationState:
    current_revision: str | None
    applied_revisions: tuple[str, ...]
    owner_id: str | None
    database_identity: str | None


def _revision_checksum() -> str:
    dialect = sqlite.dialect()
    statements = [str(CreateTable(table).compile(dialect=dialect)) for table in _upgrade_tables]
    for table in _upgrade_tables:
        statements.extend(
            str(CreateIndex(index).compile(dialect=dialect))
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        )
    material = "\n".join((MEMORY_V2_REVISION_0001, *statements))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _existing_table_names(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _read_applied(connection) -> dict[str, str]:
    if MEMORY_V2_LEDGER_TABLE not in _existing_table_names(connection):
        return {}
    rows = connection.execute(select(_ledger.c.revision, _ledger.c.revision_checksum)).all()
    return {str(row.revision): str(row.revision_checksum) for row in rows}


def _validate_applied_revisions(applied: dict[str, str]) -> None:
    unknown = set(applied) - {MEMORY_V2_REVISION_0001}
    if unknown:
        raise MemoryV2MigrationError(f"unsupported_memory_v2_revisions:{','.join(sorted(unknown))}")
    expected = _revision_checksum()
    actual = applied.get(MEMORY_V2_REVISION_0001)
    if actual is not None and actual != expected:
        raise MemoryV2MigrationError("memory_v2_revision_checksum_mismatch")


def _validate_managed_tables(connection, *, revision_applied: bool) -> None:
    existing = _existing_table_names(connection)
    managed = set(MEMORY_V2_TABLES)
    present = existing & managed
    if revision_applied and present != managed:
        missing = ",".join(sorted(managed - present))
        raise MemoryV2MigrationError(f"memory_v2_schema_missing_tables:{missing}")
    if not revision_applied and present:
        names = ",".join(sorted(present))
        raise MemoryV2MigrationError(f"unmanaged_memory_v2_tables:{names}")


def _bind_owner(connection, *, owner_id: str, database_identity: str) -> None:
    if not database_identity.strip():
        raise MemoryV2MigrationError("database_identity_required")
    rows = connection.execute(select(MemoryOwnerBindingV2.__table__)).mappings().all()
    if not rows:
        connection.execute(
            MemoryOwnerBindingV2.__table__.insert().values(
                owner_id=owner_id,
                database_identity=database_identity,
                schema_version=1,
            )
        )
        return
    if len(rows) != 1:
        raise MemoryV2MigrationError("memory_v2_database_has_multiple_owner_bindings")
    row = rows[0]
    if row["owner_id"] != owner_id or row["database_identity"] != database_identity:
        raise MemoryV2MigrationError("memory_v2_owner_database_binding_mismatch")


def upgrade_memory_v2(engine: Engine, *, owner_id: str, database_identity: str) -> str:
    """Upgrade a profile database to the current explicit v2 revision."""

    owner = canonical_uuid(owner_id)
    with engine.begin() as connection:
        existing = _existing_table_names(connection)
        ledger_exists = MEMORY_V2_LEDGER_TABLE in existing
        if not ledger_exists:
            unmanaged = set(MEMORY_V2_TABLES) & existing
            if unmanaged:
                names = ",".join(sorted(unmanaged))
                raise MemoryV2MigrationError(f"unmanaged_memory_v2_tables:{names}")
            _ledger.create(connection, checkfirst=False)

        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        revision_applied = MEMORY_V2_REVISION_0001 in applied
        _validate_managed_tables(connection, revision_applied=revision_applied)

        if not revision_applied:
            for table in _upgrade_tables:
                table.create(connection, checkfirst=False)
            connection.execute(
                _ledger.insert().values(
                    revision=MEMORY_V2_REVISION_0001,
                    revision_checksum=_revision_checksum(),
                    applied_at=datetime.now(UTC),
                )
            )

        _validate_managed_tables(connection, revision_applied=True)
        _bind_owner(connection, owner_id=owner, database_identity=database_identity)
    return MEMORY_V2_CURRENT_REVISION


def memory_v2_migration_state(engine: Engine) -> MemoryV2MigrationState:
    with engine.connect() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        ordered = tuple(revision for revision in (MEMORY_V2_REVISION_0001,) if revision in applied)
        owner_id = None
        database_identity = None
        if MemoryOwnerBindingV2.__tablename__ in _existing_table_names(connection):
            rows = connection.execute(select(MemoryOwnerBindingV2.__table__)).mappings().all()
            if len(rows) == 1:
                owner_id = str(rows[0]["owner_id"])
                database_identity = str(rows[0]["database_identity"])
        return MemoryV2MigrationState(
            current_revision=ordered[-1] if ordered else None,
            applied_revisions=ordered,
            owner_id=owner_id,
            database_identity=database_identity,
        )


def downgrade_memory_v2(engine: Engine, *, owner_id: str, database_identity: str) -> None:
    """Remove only Phase 1 v2 tables and its ledger, preserving every legacy table."""

    owner = canonical_uuid(owner_id)
    with engine.begin() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        if MEMORY_V2_REVISION_0001 not in applied:
            return
        _validate_managed_tables(connection, revision_applied=True)
        _bind_owner(connection, owner_id=owner, database_identity=database_identity)
        for table in reversed(_upgrade_tables):
            table.drop(connection, checkfirst=False)
        _ledger.drop(connection, checkfirst=False)
