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
MEMORY_REVISION_0003 = "0003_memory_scopes"
MEMORY_REVISION_0004 = "0004_memory_exclusive_slot_null_scope"
# Every revision, in order, defined once.  This list was previously repeated in
# four places and adding 0004 missed one of them -- the fast-path currency check
# -- which silently skipped the new revision on any database that already had
# 0003.  One definition, so a new revision cannot be half-added.
ALL_MEMORY_REVISIONS = (
    MEMORY_REVISION_0001,
    MEMORY_REVISION_0002,
    MEMORY_REVISION_0003,
    MEMORY_REVISION_0004,
)
MEMORY_CURRENT_REVISION = ALL_MEMORY_REVISIONS[-1]
# Revision checksums describe the schema at the time a revision shipped.  Keep
# this value fixed: compiling the current ORM model would otherwise make every
# existing 0001 ledger entry appear corrupt after a later column is added.
_MEMORY_REVISION_0001_CHECKSUM = "c1808b6cacd6090fea76df53bd6d07903efb532f7b47bb0e64063e88cf7bf6db"
MEMORY_LEDGER_TABLE = "memory_schema_migrations"
MEMORY_FTS5_TABLE = "memory_fts_index"
# Matches what SQLAlchemy emits for the model index, so a database reaching this
# schema by migration and one created fresh from the model agree exactly.
_EXCLUSIVE_SLOT_INDEX_SQL = (
    "CREATE UNIQUE INDEX uq_memory_records_active_exclusive_slot "
    "ON memory_records (owner_id, scope_type, COALESCE(scope_project_id, ''), "
    "subject_key, memory_type, domain_key, slot_key) "
    "WHERE status = 'active' AND cardinality = 'exclusive'"
)
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
    if revision == MEMORY_REVISION_0001:
        return _MEMORY_REVISION_0001_CHECKSUM
    if revision == MEMORY_REVISION_0003:
        material = "\n".join(
            (
                revision,
                "ALTER TABLE memory_records ADD COLUMN scope_type VARCHAR(16) NOT NULL DEFAULT 'global'",
                "ALTER TABLE memory_records ADD COLUMN scope_project_id VARCHAR(80)",
                "ALTER TABLE memory_candidates ADD COLUMN scope_type VARCHAR(16) NOT NULL DEFAULT 'global'",
                "ALTER TABLE memory_candidates ADD COLUMN scope_project_id VARCHAR(80)",
                "DROP INDEX uq_memory_records_active_exclusive_slot",
                "CREATE UNIQUE INDEX uq_memory_records_active_exclusive_slot ON memory_records (owner_id, scope_type, scope_project_id, subject_key, memory_type, domain_key, slot_key) WHERE status = 'active' AND cardinality = 'exclusive'",
                "CREATE INDEX ix_memory_records_owner_scope ON memory_records (owner_id, status, scope_type, scope_project_id)",
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    if revision == MEMORY_REVISION_0004:
        material = "\n".join(
            (
                revision,
                "DROP INDEX uq_memory_records_active_exclusive_slot",
                _EXCLUSIVE_SLOT_INDEX_SQL,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
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
    unknown = set(applied) - set(ALL_MEMORY_REVISIONS)
    if unknown:
        raise MemoryMigrationError(f"unsupported_memory_revisions:{','.join(sorted(unknown))}")
    for revision in ALL_MEMORY_REVISIONS:
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
    # This function is reached when every chat generation and memory operation
    # starts.  Most calls are already on the current revision, so do not take a
    # SQLite write reservation merely to inspect a stable schema.  In particular,
    # ``BEGIN IMMEDIATE`` here used to contend with the chat worker's own status
    # writes and could make an otherwise ordinary message fail with "database is
    # locked".  Only a genuinely pending migration needs writer serialization.
    with engine.connect() as connection:
        if _memory_schema_is_current(
            connection,
            owner_id=owner,
            database_identity=database_identity,
        ):
            return MEMORY_CURRENT_REVISION
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


def _memory_schema_is_current(
    connection,
    *,
    owner_id: str,
    database_identity: str,
) -> bool:
    """Return whether the canonical memory schema is already ready to use.

    The check intentionally performs no DDL or mutation.  If any invariant is
    invalid, retain the existing migration error behaviour instead of silently
    accepting a database bound to another profile.
    """

    existing = _existing_table_names(connection)
    if MEMORY_LEDGER_TABLE not in existing:
        return False
    applied = _read_applied(connection)
    _validate_applied_revisions(applied)
    _validate_managed_tables(connection, applied=applied)
    if set(ALL_MEMORY_REVISIONS) - set(applied):
        return False
    rows = connection.execute(select(MemoryOwnerBinding.__table__)).mappings().all()
    if not rows:
        return False
    if len(rows) != 1:
        raise MemoryMigrationError("memory_database_has_multiple_owner_bindings")
    row = rows[0]
    if row["owner_id"] != owner_id or row["database_identity"] != database_identity:
        raise MemoryMigrationError("memory_owner_database_binding_mismatch")
    return True


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
        # Earlier Neo releases used these names for a different, pre-ledger
        # memory prototype.  Preserve the rows for later import, but free the
        # canonical table names so the current schema can be installed.
        for name in ("memory_candidates", "memory_sources"):
            if name in unmanaged:
                connection.execute(text(f"ALTER TABLE {name} RENAME TO legacy_{name}"))
                unmanaged.remove(name)
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

    if MEMORY_REVISION_0003 not in applied:
        for table_name in ("memory_records", "memory_candidates"):
            columns = {column["name"] for column in inspect(connection).get_columns(table_name)}
            if "scope_type" not in columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN scope_type VARCHAR(16) NOT NULL DEFAULT 'global'"
                    )
                )
            if "scope_project_id" not in columns:
                connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN scope_project_id VARCHAR(80)")
                )
        connection.execute(text("DROP INDEX IF EXISTS uq_memory_records_active_exclusive_slot"))
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_records_active_exclusive_slot ON memory_records (owner_id, scope_type, scope_project_id, subject_key, memory_type, domain_key, slot_key) WHERE status = 'active' AND cardinality = 'exclusive'"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_memory_records_owner_scope ON memory_records (owner_id, status, scope_type, scope_project_id)"
            )
        )
        connection.execute(
            _ledger.insert().values(
                revision=MEMORY_REVISION_0003,
                revision_checksum=_revision_checksum(MEMORY_REVISION_0003),
                applied_at=datetime.now(UTC),
            )
        )
        applied[MEMORY_REVISION_0003] = _revision_checksum(MEMORY_REVISION_0003)

    if MEMORY_REVISION_0004 not in applied:
        # Revision 0003 added the nullable `scope_project_id` to this unique
        # index.  A unique index treats NULLs as distinct, so the index stopped
        # firing for every globally-scoped record -- names, preferences, primary
        # goals, current job and education -- and two contradictory active
        # records could occupy one exclusive slot.  Folding NULL to '' restores
        # the guarantee; project-scoped rows already had a non-NULL value and
        # are unaffected.
        #
        # Creating a unique index fails against data that already violates it,
        # so any existing duplicates are reported by name rather than the raw
        # SQLite error, which names neither the table nor the rows.
        duplicates = connection.execute(
            text(
                "SELECT group_concat(id) FROM memory_records "
                "WHERE status = 'active' AND cardinality = 'exclusive' "
                "GROUP BY owner_id, scope_type, COALESCE(scope_project_id, ''), "
                "subject_key, memory_type, domain_key, slot_key "
                "HAVING count(*) > 1"
            )
        ).all()
        if duplicates:
            groups = "; ".join(str(row[0]) for row in duplicates)
            raise MemoryMigrationError(f"memory_exclusive_slot_duplicates_block_migration:{groups}")
        connection.execute(text("DROP INDEX IF EXISTS uq_memory_records_active_exclusive_slot"))
        connection.execute(text(_EXCLUSIVE_SLOT_INDEX_SQL))
        connection.execute(
            _ledger.insert().values(
                revision=MEMORY_REVISION_0004,
                revision_checksum=_revision_checksum(MEMORY_REVISION_0004),
                applied_at=datetime.now(UTC),
            )
        )
        applied[MEMORY_REVISION_0004] = _revision_checksum(MEMORY_REVISION_0004)

    _validate_managed_tables(connection, applied=applied)
    _bind_owner(connection, owner_id=owner_id, database_identity=database_identity)


def memory_migration_state(engine: Engine) -> MemoryMigrationState:
    with engine.connect() as connection:
        applied = _read_applied(connection)
        _validate_applied_revisions(applied)
        ordered = tuple(revision for revision in ALL_MEMORY_REVISIONS if revision in applied)
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
