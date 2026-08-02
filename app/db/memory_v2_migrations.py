"""Explicit migration ledger for the Phase 1 memory-v2 schema.

The project does not currently depend on Alembic. This small equivalent ledger is
deliberately scoped to memory v2: ordered immutable revisions, checksums, atomic
upgrade/downgrade functions, unknown-schema refusal, and no opportunistic repair.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, MetaData, String, Table, inspect, select, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.core.identifiers import canonical_uuid
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryDerivedMetricV2,
    MemoryDerivedStateV2,
    MemoryFtsDocumentV2,
    MemoryLegacyMapV2,
    MemoryMigrationRunV2,
    MemoryOperationV2,
    MemoryOutboxDeliveryV2,
    MemoryOutboxV2,
    MemoryOwnerBindingV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
    MemoryVectorPointV2,
)

MEMORY_V2_REVISION_0001 = "0001_memory_v2_phase1"
MEMORY_V2_REVISION_0002 = "0002_memory_v2_phase6_derived_indexes"
MEMORY_V2_CURRENT_REVISION = MEMORY_V2_REVISION_0002
MEMORY_V2_LEDGER_TABLE = "memory_schema_migrations_v2"
MEMORY_V2_FTS5_TABLE = "memory_fts_index_v2"
_FTS5_CREATE_SQL = (
    f"CREATE VIRTUAL TABLE {MEMORY_V2_FTS5_TABLE} USING fts5("
    "owner_id UNINDEXED, memory_id UNINDEXED, content_hash UNINDEXED, display_text)"
)

_ledger_metadata = MetaData()
_ledger = Table(
    MEMORY_V2_LEDGER_TABLE,
    _ledger_metadata,
    Column("revision", String(80), primary_key=True),
    Column("revision_checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

_phase1_tables = (
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
_phase6_tables = (
    MemoryOutboxDeliveryV2.__table__,
    MemoryDerivedStateV2.__table__,
    MemoryDerivedMetricV2.__table__,
    MemoryFtsDocumentV2.__table__,
    MemoryVectorPointV2.__table__,
)
_upgrade_tables = (*_phase1_tables, *_phase6_tables)
MEMORY_V2_TABLES = tuple(table.name for table in _upgrade_tables)


class MemoryV2MigrationError(RuntimeError):
    """Raised when a v2 schema cannot be upgraded or downgraded safely."""


@dataclass(frozen=True)
class MemoryV2MigrationState:
    current_revision: str | None
    applied_revisions: tuple[str, ...]
    owner_id: str | None
    database_identity: str | None


def _revision_checksum(revision: str) -> str:
    dialect = sqlite.dialect()
    tables = _phase1_tables if revision == MEMORY_V2_REVISION_0001 else _phase6_tables
    statements = [str(CreateTable(table).compile(dialect=dialect)) for table in tables]
    if revision == MEMORY_V2_REVISION_0002:
        statements.append(_FTS5_CREATE_SQL)
    for table in tables:
        statements.extend(
            str(CreateIndex(index).compile(dialect=dialect))
            for index in sorted(table.indexes, key=lambda item: item.name or "")
        )
    material = "\n".join((revision, *statements))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _existing_table_names(connection) -> set[str]:
    return set(inspect(connection).get_table_names())


def _read_applied(connection) -> dict[str, str]:
    if MEMORY_V2_LEDGER_TABLE not in _existing_table_names(connection):
        return {}
    rows = connection.execute(select(_ledger.c.revision, _ledger.c.revision_checksum)).all()
    return {str(row.revision): str(row.revision_checksum) for row in rows}


def _validate_applied_revisions(applied: dict[str, str]) -> None:
    unknown = set(applied) - {MEMORY_V2_REVISION_0001, MEMORY_V2_REVISION_0002}
    if unknown:
        raise MemoryV2MigrationError(f"unsupported_memory_v2_revisions:{','.join(sorted(unknown))}")
    for revision in (MEMORY_V2_REVISION_0001, MEMORY_V2_REVISION_0002):
        actual = applied.get(revision)
        if actual is not None and actual != _revision_checksum(revision):
            raise MemoryV2MigrationError("memory_v2_revision_checksum_mismatch")


def _validate_managed_tables(connection, *, applied: dict[str, str]) -> None:
    existing = _existing_table_names(connection)
    for revision, tables in (
        (MEMORY_V2_REVISION_0001, _phase1_tables),
        (MEMORY_V2_REVISION_0002, _phase6_tables),
    ):
        managed = {table.name for table in tables}
        present = existing & managed
        if revision in applied and present != managed:
            missing = ",".join(sorted(managed - present))
            raise MemoryV2MigrationError(f"memory_v2_schema_missing_tables:{missing}")
        if revision not in applied and present:
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
        _validate_managed_tables(connection, applied=applied)

        if MEMORY_V2_REVISION_0001 not in applied:
            for table in _phase1_tables:
                table.create(connection, checkfirst=False)
            connection.execute(
                _ledger.insert().values(
                    revision=MEMORY_V2_REVISION_0001,
                    revision_checksum=_revision_checksum(MEMORY_V2_REVISION_0001),
                    applied_at=datetime.now(UTC),
                )
            )
            applied[MEMORY_V2_REVISION_0001] = _revision_checksum(MEMORY_V2_REVISION_0001)

        if MEMORY_V2_REVISION_0002 not in applied:
            for table in _phase6_tables:
                table.create(connection, checkfirst=False)
            try:
                connection.execute(text(_FTS5_CREATE_SQL))
            except OperationalError as exc:
                if "fts5" not in str(exc).casefold():
                    raise
            connection.execute(
                _ledger.insert().values(
                    revision=MEMORY_V2_REVISION_0002,
                    revision_checksum=_revision_checksum(MEMORY_V2_REVISION_0002),
                    applied_at=datetime.now(UTC),
                )
            )
            applied[MEMORY_V2_REVISION_0002] = _revision_checksum(MEMORY_V2_REVISION_0002)

        _validate_managed_tables(connection, applied=applied)
        _bind_owner(connection, owner_id=owner, database_identity=database_identity)
    return MEMORY_V2_CURRENT_REVISION


def memory_v2_migration_state(engine: Engine) -> MemoryV2MigrationState:
    with engine.connect() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        ordered = tuple(
            revision
            for revision in (MEMORY_V2_REVISION_0001, MEMORY_V2_REVISION_0002)
            if revision in applied
        )
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
        _validate_managed_tables(connection, applied=applied)
        _bind_owner(connection, owner_id=owner, database_identity=database_identity)
        connection.execute(text(f"DROP TABLE IF EXISTS {MEMORY_V2_FTS5_TABLE}"))
        for table in reversed(_upgrade_tables):
            table.drop(connection, checkfirst=False)
        _ledger.drop(connection, checkfirst=False)
