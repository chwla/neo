"""Tier 3 — the schema ledger (plan section MIG).

Migrations run on every chat generation and every memory operation, not just at
install time, so "already current" is by far the hottest path through this
module.  The tests below care about two things: that a fresh database ends up
with exactly the schema the code expects, and that a database belonging to
somebody else is refused rather than quietly adopted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text, update
from sqlalchemy.engine import Engine

from app.db.memory_migrations import (
    ALL_MEMORY_REVISIONS,
    MEMORY_CURRENT_REVISION,
    MEMORY_LEDGER_TABLE,
    MEMORY_REVISION_0001,
    MEMORY_REVISION_0004,
    MEMORY_TABLES,
    MemoryMigrationError,
    downgrade_memory,
    memory_migration_state,
    upgrade_memory,
)
from app.services.memory.diagnostics import schema_checksum
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID

# Imported rather than re-listed: a second copy here could agree with itself
# while disagreeing with the module, which is how 0004 was half-added.
ALL_REVISIONS = ALL_MEMORY_REVISIONS


def _identity(engine: Engine) -> str:
    with engine.begin() as connection:
        return connection.execute(
            text("SELECT database_identity FROM memory_owner_bindings")
        ).scalar_one()


class TestFreshUpgrade:
    def test_every_managed_table_is_created(self, unmigrated_engine: Engine) -> None:
        """MIG-01"""

        upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")
        existing = set(inspect(unmigrated_engine).get_table_names())
        assert set(MEMORY_TABLES) <= existing

    def test_the_fts5_virtual_table_is_created(self, unmigrated_engine: Engine) -> None:
        """MIG-01b — semantic and lexical recall both depend on it existing."""

        upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")
        existing = set(inspect(unmigrated_engine).get_table_names())
        assert "memory_fts_index" in existing

    def test_every_revision_is_recorded_with_a_checksum(self, unmigrated_engine: Engine) -> None:
        """MIG-02"""

        upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")
        with unmigrated_engine.begin() as connection:
            rows = connection.execute(
                text(f"SELECT revision, revision_checksum FROM {MEMORY_LEDGER_TABLE}")
            ).all()
        assert {row[0] for row in rows} == set(ALL_REVISIONS)
        for _, checksum in rows:
            assert len(checksum) == 64

    def test_the_owner_is_bound(self, unmigrated_engine: Engine) -> None:
        """MIG-03 — the binding is what every later owner check reads."""

        upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")
        with unmigrated_engine.begin() as connection:
            row = connection.execute(
                text("SELECT owner_id, database_identity FROM memory_owner_bindings")
            ).one()
        assert row == (OWNER_ID, "fresh")

    def test_the_returned_revision_is_the_current_one(self, unmigrated_engine: Engine) -> None:
        """MIG-14a"""

        revision = upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")
        assert revision == MEMORY_CURRENT_REVISION

    def test_the_current_revision_is_the_last_one(self) -> None:
        """MIG-14b — a new revision added without updating this constant is a bug."""

        assert MEMORY_CURRENT_REVISION == ALL_REVISIONS[-1]


class TestIdempotency:
    def test_running_upgrade_twice_changes_nothing(self, engine: Engine) -> None:
        """MIG-04 — this runs on every chat turn, so it must be a cheap no-op."""

        identity = _identity(engine)
        before = schema_checksum(engine)
        upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        assert schema_checksum(engine) == before

    def test_repeated_upgrades_do_not_duplicate_the_binding(self, engine: Engine) -> None:
        """MIG-04b"""

        identity = _identity(engine)
        for _ in range(3):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM memory_owner_bindings")
            ).scalar_one()
        assert count == 1

    def test_repeated_upgrades_do_not_duplicate_ledger_rows(self, engine: Engine) -> None:
        """MIG-04c"""

        identity = _identity(engine)
        upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        with engine.begin() as connection:
            count = connection.execute(
                text(f"SELECT COUNT(*) FROM {MEMORY_LEDGER_TABLE}")
            ).scalar_one()
        assert count == len(ALL_REVISIONS)


class TestBindingRefusal:
    def test_another_owner_is_refused(self, engine: Engine) -> None:
        """MIG-05 — the check that stops two profiles sharing one database."""

        identity = _identity(engine)
        with pytest.raises(MemoryMigrationError, match="binding_mismatch"):
            upgrade_memory(engine, owner_id=OTHER_OWNER_ID, database_identity=identity)

    def test_another_database_identity_is_refused(self, engine: Engine) -> None:
        """MIG-06 — a copied file must not silently become the live one."""

        with pytest.raises(MemoryMigrationError, match="binding_mismatch"):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity="/elsewhere.db")

    @pytest.mark.parametrize("identity", ["", "   "])
    def test_a_blank_identity_is_refused(self, unmigrated_engine: Engine, identity: str) -> None:
        """MIG-06b"""

        with pytest.raises(MemoryMigrationError, match="database_identity_required"):
            upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity=identity)

    def test_a_malformed_owner_is_refused(self, unmigrated_engine: Engine) -> None:
        """MIG-06c"""

        with pytest.raises(ValueError, match="canonical_uuid_required"):
            upgrade_memory(unmigrated_engine, owner_id="not-a-uuid", database_identity="fresh")

    def test_two_bindings_are_refused(self, engine: Engine) -> None:
        """MIG-06d — a database can belong to exactly one owner."""

        identity = _identity(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_owner_bindings "
                    "(owner_id, database_identity, schema_version, bound_at) "
                    "VALUES (:owner, :identity, 1, CURRENT_TIMESTAMP)"
                ),
                {"owner": OTHER_OWNER_ID, "identity": identity + "-2"},
            )
        with pytest.raises(MemoryMigrationError, match="multiple_owner_bindings"):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)


class TestLedgerIntegrity:
    def test_a_tampered_checksum_is_refused(self, engine: Engine) -> None:
        """MIG-07 — a ledger that no longer describes the schema is untrustworthy."""

        identity = _identity(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE {MEMORY_LEDGER_TABLE} SET revision_checksum = :bad "
                    "WHERE revision = :revision"
                ),
                {"bad": "0" * 64, "revision": MEMORY_REVISION_0001},
            )
        with pytest.raises(MemoryMigrationError, match="checksum_mismatch"):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)

    def test_an_unknown_revision_is_refused(self, engine: Engine) -> None:
        """MIG-08 — a database from a newer build must not be downgraded into."""

        identity = _identity(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {MEMORY_LEDGER_TABLE} "
                    "(revision, revision_checksum, applied_at) "
                    "VALUES ('0004_from_the_future', :checksum, CURRENT_TIMESTAMP)"
                ),
                {"checksum": "0" * 64},
            )
        with pytest.raises(MemoryMigrationError, match="unsupported_memory_revisions"):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)

    def test_a_missing_managed_table_is_refused(self, engine: Engine) -> None:
        """MIG-09 — the ledger claims a revision the schema does not have."""

        identity = _identity(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE memory_usage_events"))
        with pytest.raises(MemoryMigrationError, match="missing_tables"):
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)

    def test_unmanaged_tables_without_a_ledger_are_refused(self, unmigrated_engine: Engine) -> None:
        """MIG-09b — a half-built database is not silently adopted."""

        from app.models.memory import MemoryRecord

        MemoryRecord.__table__.create(unmigrated_engine)
        with pytest.raises(MemoryMigrationError, match="unmanaged_memory_tables"):
            upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")


class TestRevision0004:
    """The exclusive-slot index fix, and the guard that runs before it."""

    def test_the_index_folds_a_null_scope_to_a_comparable_value(self, engine: Engine) -> None:
        """MIG-15 — the shape of the fixed index, read from the database itself.

        Asserted against `sqlite_master` rather than the model, because the
        model is what *should* have been true before revision 0004 as well. What
        matters is the SQL the database is actually enforcing.
        """

        with engine.connect() as connection:
            sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name = "
                "'uq_memory_records_active_exclusive_slot'"
            ).scalar()
        assert "COALESCE(scope_project_id, '')" in sql

    def test_the_migration_refuses_to_run_against_existing_duplicates(self, engine: Engine) -> None:
        """MIG-16 — the guard that makes this safe to run on a live profile.

        Creating a unique index fails if the data already violates it, and
        SQLite's own error names neither the table nor the offending rows. Any
        store that ran a multi-target forget before `EXC-19c` was fixed has
        exactly those duplicates sitting in it, so this is not a hypothetical.

        The migration therefore looks first and raises with the row ids grouped,
        so whoever runs it can find and resolve them rather than reading a bare
        "UNIQUE constraint failed".
        """

        from sqlalchemy import text as sql_text

        from tests.memory import factories

        identity = _identity(engine)
        exclusive = {
            "memory_type": MemoryType.IDENTITY,
            "domain_key": "global",
            "slot_key": "identity:global:name",
            "cardinality": Cardinality.EXCLUSIVE,
        }
        first = factories.insert_record(engine, display_text="Soham", **exclusive)

        # Insert the duplicate the way a pre-fix store acquired one: with the
        # corrected index temporarily absent, which is the state revision 0004
        # is about to migrate away from.
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "DROP INDEX IF EXISTS uq_memory_records_active_exclusive_slot"
            )
            connection.commit()
        second = factories.insert_record(engine, display_text="Someone Else", **exclusive)
        with engine.begin() as connection:
            connection.execute(
                sql_text("DELETE FROM memory_schema_migrations WHERE revision = :r"),
                {"r": MEMORY_REVISION_0004},
            )

        with pytest.raises(MemoryMigrationError) as caught:
            upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)

        message = str(caught.value)
        assert "memory_exclusive_slot_duplicates_block_migration" in message
        assert first in message and second in message


class TestMigrationState:
    def test_state_reports_the_current_revision_and_binding(self, engine: Engine) -> None:
        """MIG-10"""

        identity = _identity(engine)
        state = memory_migration_state(engine)
        assert state.current_revision == MEMORY_CURRENT_REVISION
        assert set(state.applied_revisions) == set(ALL_REVISIONS)
        assert state.owner_id == OWNER_ID
        assert state.database_identity == identity

    def test_state_of_an_empty_database_is_empty(self, unmigrated_engine: Engine) -> None:
        """MIG-10b — reporting on a fresh file must not raise."""

        state = memory_migration_state(unmigrated_engine)
        assert state.current_revision is None
        assert state.applied_revisions == ()
        assert state.owner_id is None


class TestDowngrade:
    def test_downgrade_removes_every_managed_table(self, engine: Engine) -> None:
        """MIG-11"""

        identity = _identity(engine)
        downgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        existing = set(inspect(engine).get_table_names())
        assert not (set(MEMORY_TABLES) & existing)

    def test_downgrade_clears_the_ledger(self, engine: Engine) -> None:
        """MIG-11b"""

        identity = _identity(engine)
        downgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        assert memory_migration_state(engine).applied_revisions == ()

    def test_downgrade_then_upgrade_restores_the_same_schema(self, engine: Engine) -> None:
        """MIG-12 — the round trip has to be exact, or a rebuild diverges."""

        identity = _identity(engine)
        before = schema_checksum(engine)
        downgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
        assert schema_checksum(engine) == before

    def test_downgrade_refuses_another_owner(self, engine: Engine) -> None:
        """MIG-11c — dropping someone else's tables must be impossible."""

        identity = _identity(engine)
        with pytest.raises(MemoryMigrationError, match="binding_mismatch"):
            downgrade_memory(engine, owner_id=OTHER_OWNER_ID, database_identity=identity)


class TestAtomicity:
    def test_a_failed_upgrade_leaves_no_partial_schema(
        self, unmigrated_engine: Engine, monkeypatch
    ) -> None:
        """MIG-13 — a crash mid-migration must not leave a half-built database.

        The upgrade runs inside one ``BEGIN IMMEDIATE`` transaction, so an
        exception part-way through should roll back every table created so far
        rather than leaving a database that is neither empty nor current.
        """

        import app.db.memory_migrations as migrations

        original = migrations._bind_owner

        def _explode(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated failure after DDL")

        monkeypatch.setattr(migrations, "_bind_owner", _explode)

        with pytest.raises(RuntimeError, match="simulated failure"):
            upgrade_memory(unmigrated_engine, owner_id=OWNER_ID, database_identity="fresh")

        existing = set(inspect(unmigrated_engine).get_table_names())
        assert not (set(MEMORY_TABLES) & existing)
        assert MEMORY_LEDGER_TABLE not in existing


class TestSchemaChecksum:
    def test_the_checksum_is_stable(self, engine: Engine) -> None:
        """MIG-12b — used by the round-trip test above, so pin it directly."""

        assert schema_checksum(engine) == schema_checksum(engine)

    def test_the_checksum_changes_when_the_schema_changes(self, engine: Engine) -> None:
        """MIG-12c"""

        before = schema_checksum(engine)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE extra_table (id TEXT PRIMARY KEY)"))
        assert schema_checksum(engine) != before


class TestScopeMigration:
    """MIG-01c — the 0003 revision that added project scoping."""

    def test_records_and_candidates_both_gained_scope_columns(self, engine: Engine) -> None:
        inspector = inspect(engine)
        for table in ("memory_records", "memory_candidates"):
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert {"scope_type", "scope_project_id"} <= columns, table

    def test_scope_type_defaults_to_global(self, engine: Engine) -> None:
        """An existing record from before scoping must remain globally visible."""

        from tests.memory import factories

        operation_id = factories.insert_operation(engine)
        values = factories.record_values(operation_id=operation_id)
        values.pop("scope_type")
        values.pop("scope_project_id")
        with engine.begin() as connection:
            from app.models.memory import MemoryRecord

            connection.execute(MemoryRecord.__table__.insert().values(**values))
            scope = connection.execute(
                text("SELECT scope_type FROM memory_records WHERE id = :id"),
                {"id": values["id"]},
            ).scalar_one()
        assert scope == "global"

    def test_the_exclusive_slot_index_exists(self, engine: Engine) -> None:
        """The index SCH-14 shows does not fire at global scope — but it is present.

        Worth asserting separately so that the SCH-14 failure cannot be
        misread as "the migration forgot to create the index".
        """

        with engine.begin() as connection:
            sql = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name = 'uq_memory_records_active_exclusive_slot'"
                )
            ).scalar_one()
        assert "scope_project_id" in sql
        assert "WHERE status = 'active' AND cardinality = 'exclusive'" in sql


def test_the_ledger_survives_unrelated_writes(engine: Engine) -> None:
    """MIG-04d — ordinary traffic must not disturb the schema ledger."""

    from app.models.memory import MemoryRecord
    from tests.memory import factories

    identity = _identity(engine)
    record_id = factories.insert_record(engine)
    with engine.begin() as connection:
        connection.execute(
            update(MemoryRecord).where(MemoryRecord.id == record_id).values(importance=6)
        )
    upgrade_memory(engine, owner_id=OWNER_ID, database_identity=identity)
    assert memory_migration_state(engine).current_revision == MEMORY_CURRENT_REVISION
