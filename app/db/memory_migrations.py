"""Explicit schema ledger for Neo's canonical memory database objects."""

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
from app.models.memory import (
    MemoryCandidate,
    MemoryDerivedMetric,
    MemoryDerivedState,
    MemoryFtsDocument,
    MemoryOperation,
    MemoryOutbox,
    MemoryOutboxDelivery,
    MemoryOwnerBinding,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryTombstone,
    MemoryUsageEvent,
    MemoryVectorPoint,
)

MEMORY_REVISION_0001 = "0001_memory_core"
MEMORY_REVISION_0002 = "0002_memory_derived_indexes"
MEMORY_CURRENT_REVISION = MEMORY_REVISION_0002
MEMORY_LEDGER_TABLE = "memory_schema_migrations"
MEMORY_FTS5_TABLE = "memory_fts_index"
_FTS5_CREATE_SQL = (
    f"CREATE VIRTUAL TABLE {MEMORY_FTS5_TABLE} USING fts5("
    "owner_id UNINDEXED, memory_id UNINDEXED, content_hash UNINDEXED, display_text)"
)

_ledger_metadata = MetaData()
_ledger = Table(
    MEMORY_LEDGER_TABLE,
    _ledger_metadata,
    Column("revision", String(80), primary_key=True),
    Column("revision_checksum", String(64), nullable=False),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

_core_tables = (
    MemoryOwnerBinding.__table__,
    MemoryOperation.__table__,
    MemoryRecord.__table__,
    MemoryCandidate.__table__,
    MemorySource.__table__,
    MemoryRelation.__table__,
    MemoryUsageEvent.__table__,
    MemoryOutbox.__table__,
    MemoryTombstone.__table__,
)
_derived_tables = (
    MemoryOutboxDelivery.__table__,
    MemoryDerivedState.__table__,
    MemoryDerivedMetric.__table__,
    MemoryFtsDocument.__table__,
    MemoryVectorPoint.__table__,
)
_upgrade_tables = (*_core_tables, *_derived_tables)
MEMORY_TABLES = tuple(table.name for table in _upgrade_tables)


class MemoryMigrationError(RuntimeError):
    """Raised when a memory schema cannot be upgraded or downgraded safely."""


@dataclass(frozen=True)
class MemoryMigrationState:
    current_revision: str | None
    applied_revisions: tuple[str, ...]
    owner_id: str | None
    database_identity: str | None


def _revision_checksum(revision: str) -> str:
    dialect = sqlite.dialect()
    tables = _core_tables if revision == MEMORY_REVISION_0001 else _derived_tables
    statements = [str(CreateTable(table).compile(dialect=dialect)) for table in tables]
    if revision == MEMORY_REVISION_0002:
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
    if MEMORY_LEDGER_TABLE not in _existing_table_names(connection):
        return {}
    rows = connection.execute(select(_ledger.c.revision, _ledger.c.revision_checksum)).all()
    return {str(row.revision): str(row.revision_checksum) for row in rows}


def _validate_applied_revisions(applied: dict[str, str]) -> None:
    unknown = set(applied) - {MEMORY_REVISION_0001, MEMORY_REVISION_0002}
    if unknown:
        raise MemoryMigrationError(f"unsupported_memory_revisions:{','.join(sorted(unknown))}")
    for revision in (MEMORY_REVISION_0001, MEMORY_REVISION_0002):
        actual = applied.get(revision)
        if actual is not None and actual != _revision_checksum(revision):
            raise MemoryMigrationError("memory_revision_checksum_mismatch")


def _validate_managed_tables(connection, *, applied: dict[str, str]) -> None:
    existing = _existing_table_names(connection)
    for revision, tables in (
        (MEMORY_REVISION_0001, _core_tables),
        (MEMORY_REVISION_0002, _derived_tables),
    ):
        managed = {table.name for table in tables}
        present = existing & managed
        if revision in applied and present != managed:
            missing = ",".join(sorted(managed - present))
            raise MemoryMigrationError(f"memory_schema_missing_tables:{missing}")
        if revision not in applied and present:
            names = ",".join(sorted(present))
            raise MemoryMigrationError(f"unmanaged_memory_tables:{names}")


def _bind_owner(connection, *, owner_id: str, database_identity: str) -> None:
    if not database_identity.strip():
        raise MemoryMigrationError("database_identity_required")
    rows = connection.execute(select(MemoryOwnerBinding.__table__)).mappings().all()
    if not rows:
        connection.execute(
            MemoryOwnerBinding.__table__.insert().values(
                owner_id=owner_id,
                database_identity=database_identity,
                schema_version=1,
            )
        )
        return
    if len(rows) != 1:
        raise MemoryMigrationError("memory_database_has_multiple_owner_bindings")
    row = rows[0]
    if row["owner_id"] != owner_id or row["database_identity"] != database_identity:
        raise MemoryMigrationError("memory_owner_database_binding_mismatch")


def upgrade_memory(engine: Engine, *, owner_id: str, database_identity: str) -> str:
    """Upgrade a profile database to the current explicit memory revision."""

    owner = canonical_uuid(owner_id)
    # Schema inspection and DDL must be one writer-serialized unit.  A deferred
    # SQLite transaction lets two first requests both observe a missing ledger,
    # then one fails creating it with ``table ... already exists``.  Taking the
    # write reservation before inspecting makes initialisation safe for parallel
    # API requests as well as workers starting at the same time.
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            _upgrade_memory_in_transaction(
                connection,
                owner_id=owner,
                database_identity=database_identity,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return MEMORY_CURRENT_REVISION


def _upgrade_memory_in_transaction(connection, *, owner_id: str, database_identity: str) -> None:
    """Apply the memory schema while the caller owns a SQLite write reservation."""

    # ``upgrade_memory`` deliberately opens this transaction with BEGIN IMMEDIATE;
    # keep all inspection and DDL below that boundary.
    if connection.dialect.name != "sqlite":
        raise MemoryMigrationError("memory_migrations_require_sqlite")

    
    existing = _existing_table_names(connection)
    ledger_exists = MEMORY_LEDGER_TABLE in existing
    if not ledger_exists:
        unmanaged = set(MEMORY_TABLES) & existing
        if unmanaged:
            names = ",".join(sorted(unmanaged))
            raise MemoryMigrationError(f"unmanaged_memory_tables:{names}")
        _ledger.create(connection, checkfirst=False)

    applied = _read_applied(connection)
    _validate_applied_revisions(applied)
    _validate_managed_tables(connection, applied=applied)

    if MEMORY_REVISION_0001 not in applied:
        for table in _core_tables:
            table.create(connection, checkfirst=False)
        connection.execute(
            _ledger.insert().values(
                revision=MEMORY_REVISION_0001,
                revision_checksum=_revision_checksum(MEMORY_REVISION_0001),
                applied_at=datetime.now(UTC),
            )
        )
        applied[MEMORY_REVISION_0001] = _revision_checksum(MEMORY_REVISION_0001)

    if MEMORY_REVISION_0002 not in applied:
        for table in _derived_tables:
            table.create(connection, checkfirst=False)
        try:
            connection.execute(text(_FTS5_CREATE_SQL))
        except OperationalError as exc:
            if "fts5" not in str(exc).casefold():
                raise
        connection.execute(
            _ledger.insert().values(
                revision=MEMORY_REVISION_0002,
                revision_checksum=_revision_checksum(MEMORY_REVISION_0002),
                applied_at=datetime.now(UTC),
            )
        )
        applied[MEMORY_REVISION_0002] = _revision_checksum(MEMORY_REVISION_0002)

    _validate_managed_tables(connection, applied=applied)
    _bind_owner(connection, owner_id=owner_id, database_identity=database_identity)


def memory_migration_state(engine: Engine) -> MemoryMigrationState:
    with engine.connect() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        ordered = tuple(
            revision
            for revision in (MEMORY_REVISION_0001, MEMORY_REVISION_0002)
            if revision in applied
        )
        owner_id = None
        database_identity = None
        if MemoryOwnerBinding.__tablename__ in _existing_table_names(connection):
            rows = connection.execute(select(MemoryOwnerBinding.__table__)).mappings().all()
            if len(rows) == 1:
                owner_id = str(rows[0]["owner_id"])
                database_identity = str(rows[0]["database_identity"])
        return MemoryMigrationState(
            current_revision=ordered[-1] if ordered else None,
            applied_revisions=ordered,
            owner_id=owner_id,
            database_identity=database_identity,
        )


def downgrade_memory(engine: Engine, *, owner_id: str, database_identity: str) -> None:
    """Remove only canonical memory tables and their schema ledger."""

    owner = canonical_uuid(owner_id)
    with engine.begin() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        if MEMORY_REVISION_0001 not in applied:
            return
        _validate_managed_tables(connection, applied=applied)
        _bind_owner(connection, owner_id=owner, database_identity=database_identity)
        connection.execute(text(f"DROP TABLE IF EXISTS {MEMORY_FTS5_TABLE}"))
        for table in reversed(_upgrade_tables):
            table.drop(connection, checkfirst=False)
        _ledger.drop(connection, checkfirst=False)
