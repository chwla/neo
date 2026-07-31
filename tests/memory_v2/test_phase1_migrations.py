from __future__ import annotations

from sqlalchemy import inspect, text

import app.models  # noqa: F401
from app.db.base import Base
from app.db.memory_v2_migrations import (
    MEMORY_V2_CURRENT_REVISION,
    MEMORY_V2_LEDGER_TABLE,
    MEMORY_V2_TABLES,
    MemoryV2MigrationError,
    downgrade_memory_v2,
    memory_v2_migration_state,
    upgrade_memory_v2,
)
from app.db.session import build_engine
from tests.memory_v2.factories import DATABASE_IDENTITY, OWNER_A


def test_empty_database_upgrade_creates_complete_versioned_schema(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    try:
        revision = upgrade_memory_v2(
            engine,
            owner_id=OWNER_A,
            database_identity=DATABASE_IDENTITY,
        )
        tables = set(inspect(engine).get_table_names())
        assert revision == MEMORY_V2_CURRENT_REVISION
        assert set(MEMORY_V2_TABLES) <= tables
        assert MEMORY_V2_LEDGER_TABLE in tables
        assert memory_v2_migration_state(engine).current_revision == MEMORY_V2_CURRENT_REVISION
        assert not any(name.endswith("_v2") for name in Base.metadata.tables)
    finally:
        engine.dispose()


def test_upgrade_is_idempotent(memory_v2_engine) -> None:
    first = memory_v2_migration_state(memory_v2_engine)
    upgrade_memory_v2(
        memory_v2_engine,
        owner_id=OWNER_A,
        database_identity=DATABASE_IDENTITY,
    )
    second = memory_v2_migration_state(memory_v2_engine)
    assert first == second
    with memory_v2_engine.connect() as connection:
        count = connection.scalar(text(f"SELECT count(*) FROM {MEMORY_V2_LEDGER_TABLE}"))
    assert count == 1


def test_upgrade_preserves_minimal_legacy_schema_and_data(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'legacy-minimal.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE memories (id INTEGER PRIMARY KEY, memory_text TEXT NOT NULL)")
            )
            connection.execute(
                text("INSERT INTO memories (id, memory_text) VALUES (1, 'legacy fact')")
            )
        before_columns = [column["name"] for column in inspect(engine).get_columns("memories")]

        upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)

        after_columns = [column["name"] for column in inspect(engine).get_columns("memories")]
        with engine.connect() as connection:
            value = connection.scalar(text("SELECT memory_text FROM memories WHERE id = 1"))
        assert after_columns == before_columns
        assert value == "legacy fact"
    finally:
        engine.dispose()


def test_upgrade_preserves_current_legacy_metadata_fixture(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'legacy-current.db'}")
    try:
        Base.metadata.create_all(engine)
        legacy_tables = set(inspect(engine).get_table_names())
        upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)
        assert legacy_tables <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_explicit_downgrade_removes_only_v2_and_preserves_legacy(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE legacy_marker (id INTEGER PRIMARY KEY, value TEXT)")
            )
            connection.execute(text("INSERT INTO legacy_marker VALUES (1, 'preserve me')"))
        upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)

        downgrade_memory_v2(
            engine,
            owner_id=OWNER_A,
            database_identity=DATABASE_IDENTITY,
        )

        tables = set(inspect(engine).get_table_names())
        assert not (set(MEMORY_V2_TABLES) & tables)
        assert MEMORY_V2_LEDGER_TABLE not in tables
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM legacy_marker WHERE id = 1")) == (
                "preserve me"
            )
    finally:
        engine.dispose()


def test_wrong_owner_or_database_identity_fails_closed(memory_v2_engine) -> None:
    try:
        upgrade_memory_v2(
            memory_v2_engine,
            owner_id="00000000-0000-4000-8000-000000000002",
            database_identity=DATABASE_IDENTITY,
        )
    except MemoryV2MigrationError as exc:
        assert str(exc) == "memory_v2_owner_database_binding_mismatch"
    else:
        raise AssertionError("wrong owner binding was accepted")


def test_unmanaged_partial_v2_schema_fails_safely(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'partial.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE memory_records_v2 (id TEXT PRIMARY KEY)"))
        try:
            upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)
        except MemoryV2MigrationError as exc:
            assert str(exc).startswith("unmanaged_memory_v2_tables:")
        else:
            raise AssertionError("partial unmanaged v2 schema was accepted")
    finally:
        engine.dispose()


def test_unknown_migration_revision_fails_safely(tmp_path) -> None:
    engine = build_engine(f"sqlite:///{tmp_path / 'unknown-revision.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE TABLE {MEMORY_V2_LEDGER_TABLE} ("
                    "revision TEXT PRIMARY KEY, revision_checksum TEXT NOT NULL, "
                    "applied_at DATETIME NOT NULL)"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {MEMORY_V2_LEDGER_TABLE} "
                    "VALUES ('9999_unknown', 'unknown', CURRENT_TIMESTAMP)"
                )
            )
        try:
            upgrade_memory_v2(engine, owner_id=OWNER_A, database_identity=DATABASE_IDENTITY)
        except MemoryV2MigrationError as exc:
            assert str(exc).startswith("unsupported_memory_v2_revisions:")
        else:
            raise AssertionError("unknown migration revision was accepted")
    finally:
        engine.dispose()


def test_changed_revision_checksum_fails_safely(memory_v2_engine) -> None:
    with memory_v2_engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {MEMORY_V2_LEDGER_TABLE} SET revision_checksum = 'corrupt' "
                "WHERE revision = :revision"
            ),
            {"revision": MEMORY_V2_CURRENT_REVISION},
        )
    try:
        upgrade_memory_v2(
            memory_v2_engine,
            owner_id=OWNER_A,
            database_identity=DATABASE_IDENTITY,
        )
    except MemoryV2MigrationError as exc:
        assert str(exc) == "memory_v2_revision_checksum_mismatch"
    else:
        raise AssertionError("changed revision checksum was accepted")
