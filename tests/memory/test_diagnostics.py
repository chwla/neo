"""Tier 4 — diagnostics, checksums and invariant inspection (plan section DIA).

This module answers "is this profile's database sound?" — and it is the only
thing that can, because several invariants the schema is supposed to enforce
turn out not to be enforced in practice.

The most important test here is `DIA-15`. The `SCH-14` defect means two active
records can occupy one exclusive slot: the unique index stops firing at global
scope because migration 0003 added a nullable column to it. `inspect_memory_
invariants` runs its own GROUP BY and catches exactly that. So the defect has a
*detector* even though it has no *preventer*, which is worth knowing precisely,
because it is the difference between "silently wrong" and "wrong but findable".

Checksums get the same treatment as everywhere else in this suite: stable for
identical input, moving for changed input, and scoped to one owner. A checksum
that is merely stable is satisfied by returning a constant.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.services.memory.diagnostics import (
    _require_sqlite,
    canonical_data_checksum,
    create_sqlite_backup,
    identify_database_owner,
    inspect_memory_invariants,
    run_sqlite_integrity_check,
    schema_checksum,
)
from app.services.memory.index_contracts import DerivedMetricCode
from app.services.memory.metrics import MemoryDerivedMetrics
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID
from tests.memory.factories import insert_operation, insert_record


@pytest.fixture
def metrics(engine, tmp_path) -> MemoryDerivedMetrics:
    return MemoryDerivedMetrics(
        engine, owner_id=OWNER_ID, database_identity=str(tmp_path / "memory.db")
    )


class TestDerivedMetrics:
    def test_recording_a_metric_then_reading_it_back(self, metrics) -> None:
        """DIA-01"""

        metrics.record({DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT: 2})
        assert metrics.snapshot()[DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT] == 2

    def test_recording_the_same_code_accumulates(self, metrics) -> None:
        """DIA-01b — counters, not gauges.

        Workers record increments from separate processes. Overwriting rather
        than adding would mean the count reflects whichever worker wrote last.
        """

        metrics.record({DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT: 2})
        metrics.record({DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT: 3})
        assert metrics.snapshot()[DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT] == 5

    def test_an_unknown_metric_code_is_refused(self, metrics) -> None:
        """DIA-02 — a typo would otherwise create a counter nobody reads."""

        with pytest.raises(ValueError):
            metrics.record({"not_a_real_metric": 1})

    def test_the_snapshot_reports_only_recorded_codes(self, metrics) -> None:
        """DIA-04 — pinning what it does, which is not what the plan assumed.

        I expected a zero-filled dict of every code. It returns only codes that
        have actually been recorded, so an untouched store gives `{}`.

        Defensible, and I left it: an absent key means "never happened" while a
        present zero would mean "happened and was counted as zero", and for
        these particular counters — semantic hits dropped for wrong owner, stale,
        ghost, inactive — that distinction is worth keeping. A dashboard showing
        blank rather than 0 for a metric that has never fired is arguably more
        honest.

        The cost is that every consumer must write `.get(code, 0)`, and the one
        that forgets renders a blank. Recorded here so the requirement is
        visible rather than discovered.
        """

        assert metrics.snapshot() == {}
        metrics.record({DerivedMetricCode.SEMANTIC_STALE_HIT_DROP: 1})
        snapshot = metrics.snapshot()
        assert set(snapshot) == {DerivedMetricCode.SEMANTIC_STALE_HIT_DROP}

    def test_another_owner_cannot_read_these_metrics_at_all(
        self, engine, tmp_path, metrics
    ) -> None:
        """DIA-03 — isolation here is stronger than "returns nothing".

        I expected a foreign-owner reader to see zeros. It cannot be used at
        all: every call re-checks the database's owner binding and refuses when
        it does not match. So the answer is not an empty result that a caller
        might mistake for "no activity" — it is a hard error.

        That is the better design for a per-profile database. A reader silently
        returning zeros against the wrong profile looks exactly like a healthy
        idle profile.
        """

        from app.repositories.memory import MemoryBindingError

        metrics.record({DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT: 4})
        other = MemoryDerivedMetrics(
            engine, owner_id=OTHER_OWNER_ID, database_identity=str(tmp_path / "memory.db")
        )
        with pytest.raises(MemoryBindingError, match="binding_mismatch"):
            other.snapshot()


class TestDatabaseIdentity:
    def test_the_bound_owner_is_reported(self, engine) -> None:
        """DIA-05 — every profile database carries exactly one owner binding."""

        identity = identify_database_owner(engine)
        assert identity.owner_id == OWNER_ID
        assert identity.database_path.endswith(".db")

    def test_an_unbound_database_is_refused(self, unmigrated_engine) -> None:
        """DIA-06 — an unmigrated database has no binding, so it has no owner.

        Guessing an owner here would be the worst possible failure: every
        subsequent query would be scoped to a profile that was never checked.

        The failure is an `OperationalError` (the binding table does not exist
        yet) rather than the `ValueError` raised when the table exists but holds
        the wrong number of rows. Both refuse, which is what matters; asserting
        the broad `Exception` records that this path has two distinct causes
        rather than pretending it has one.
        """

        with pytest.raises(Exception, match="memory_owner_bindings|owner_binding"):
            identify_database_owner(unmigrated_engine)

    def test_a_database_with_no_binding_row_is_refused(self, engine) -> None:
        """DIA-06b — the migrated-but-unbound case, which is the ValueError.

        A binding table with zero or several rows means the database cannot say
        whose it is. Picking the first row would silently serve one profile's
        memories under another profile's name.
        """

        with engine.begin() as connection:
            connection.execute(text("DELETE FROM memory_owner_bindings"))
        with pytest.raises(ValueError, match="database_requires_one_owner_binding"):
            identify_database_owner(engine)

    def test_an_in_memory_database_is_refused(self) -> None:
        """DIA-07 — these operations need a real file.

        Backups copy a file and integrity checks read one. An in-memory
        database would let both appear to succeed against nothing.
        """

        memory_engine = create_engine("sqlite://")
        with pytest.raises(ValueError, match="file_backed_sqlite_database_required"):
            _require_sqlite(memory_engine)

    def test_a_non_sqlite_engine_is_refused(self) -> None:
        """DIA-07b — the PRAGMA statements here are SQLite-specific.

        Built with `create_mock_engine` so no driver is needed and nothing
        connects: the check reads `engine.dialect.name` and must refuse before
        any statement is issued.
        """

        from sqlalchemy import create_mock_engine

        postgres = create_mock_engine("postgresql://", lambda *a, **k: None)
        with pytest.raises(ValueError, match="sqlite_engine_required"):
            _require_sqlite(postgres)


class TestIntegrityAndBackup:
    def test_a_healthy_database_reports_ok(self, engine) -> None:
        """DIA-08"""

        assert run_sqlite_integrity_check(engine) == ("ok",)

    def test_a_backup_is_written_and_verified(self, engine, tmp_path) -> None:
        """DIA-09 — the manifest is the evidence the backup is usable.

        A backup nobody verified is a file, not a backup. This one runs
        `PRAGMA integrity_check` on the copy and refuses to return a manifest
        unless it passes.
        """

        destination = tmp_path / "backup.db"
        manifest = create_sqlite_backup(engine, destination)
        assert destination.exists()
        assert manifest.integrity_result == ("ok",)
        assert manifest.owner_id == OWNER_ID
        assert len(manifest.sha256) == 64

    def test_the_backup_contains_the_data(self, engine, tmp_path) -> None:
        """DIA-10 — the property that actually matters.

        A manifest with a matching checksum proves the file was copied intact.
        It does not prove the file contains the memories, which is what someone
        restoring it needs.
        """

        record_id = insert_record(engine, display_text="improve at urban sketching")
        destination = tmp_path / "backup.db"
        create_sqlite_backup(engine, destination)

        restored = sqlite3.connect(destination)
        try:
            row = restored.execute(
                "SELECT display_text FROM memory_records WHERE id = ?", (record_id,)
            ).fetchone()
        finally:
            restored.close()
        assert row is not None
        assert row[0] == "improve at urban sketching"

    def test_the_recorded_checksum_matches_the_written_file(self, engine, tmp_path) -> None:
        """DIA-09b — recomputed independently, not trusted from the manifest."""

        import hashlib

        destination = tmp_path / "backup.db"
        manifest = create_sqlite_backup(engine, destination)
        assert hashlib.sha256(destination.read_bytes()).hexdigest() == manifest.sha256

    def test_overwriting_an_existing_backup_is_refused(self, engine, tmp_path) -> None:
        """DIA-09c — a backup must never silently destroy an older one.

        This is the one destructive mistake available here, and the cost is
        asymmetric: refusing costs a filename, overwriting costs the only copy
        of a profile that may already be broken.
        """

        destination = tmp_path / "backup.db"
        create_sqlite_backup(engine, destination)
        with pytest.raises(FileExistsError):
            create_sqlite_backup(engine, destination)

    def test_backing_up_over_the_source_is_refused(self, engine) -> None:
        """DIA-09d — the same asymmetry, at its worst."""

        source = Path(identify_database_owner(engine).database_path)
        with pytest.raises(ValueError, match="backup_destination_must_differ_from_source"):
            create_sqlite_backup(engine, source)


class TestChecksums:
    def test_the_schema_checksum_is_stable(self, engine) -> None:
        """DIA-11"""

        assert schema_checksum(engine) == schema_checksum(engine)

    def test_the_schema_checksum_ignores_data(self, engine) -> None:
        """DIA-11b — schema and data are separate questions.

        A schema checksum that moved when a memory was added would be useless
        for its actual job: telling you whether two databases share a shape.
        """

        before = schema_checksum(engine)
        insert_record(engine)
        assert schema_checksum(engine) == before

    def test_the_schema_checksum_changes_with_the_schema(self, engine) -> None:
        """DIA-11c — and it must notice a real change."""

        before = schema_checksum(engine)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE extra_table (id TEXT PRIMARY KEY)"))
        assert schema_checksum(engine) != before

    def test_the_data_checksum_is_stable(self, engine) -> None:
        """DIA-12"""

        insert_record(engine)
        assert canonical_data_checksum(engine, owner_id=OWNER_ID) == canonical_data_checksum(
            engine, owner_id=OWNER_ID
        )

    def test_the_data_checksum_changes_when_data_changes(self, engine) -> None:
        """DIA-12b"""

        insert_record(engine)
        before = canonical_data_checksum(engine, owner_id=OWNER_ID)
        insert_record(engine, display_text="a different memory")
        assert canonical_data_checksum(engine, owner_id=OWNER_ID) != before

    def test_the_data_checksum_is_owner_scoped(self, engine) -> None:
        """DIA-12c — two profiles share a checksum function, not a checksum."""

        insert_record(engine)
        mine = canonical_data_checksum(engine, owner_id=OWNER_ID)
        insert_record(engine, owner=OTHER_OWNER_ID)
        assert canonical_data_checksum(engine, owner_id=OWNER_ID) == mine
        assert canonical_data_checksum(engine, owner_id=OTHER_OWNER_ID) != mine

    def test_the_data_checksum_ignores_row_order(self, engine) -> None:
        """DIA-13 — rows come back in whatever order SQLite chooses.

        Without the explicit sort, the same data would checksum differently
        after a VACUUM or an index change, and every comparison between two
        copies of one profile would report a spurious difference.
        """

        first = insert_record(engine, display_text="alpha")
        second = insert_record(engine, display_text="beta")
        before = canonical_data_checksum(engine, owner_id=OWNER_ID)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE memory_records SET updated_at = updated_at WHERE id IN (:a, :b)"),
                {"a": second, "b": first},
            )
        assert canonical_data_checksum(engine, owner_id=OWNER_ID) == before


class TestInvariants:
    def test_a_clean_store_is_healthy(self, engine) -> None:
        """DIA-14"""

        insert_record(engine)
        report = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert report.healthy is True
        assert report.violations == ()
        assert report.integrity_result == ("ok",)

    def test_two_active_exclusive_records_in_one_slot_are_detected(self, engine) -> None:
        """DIA-15 — the detector, now that the preventer exists too.

        This used to work by exploiting `SCH-14`: the unique index did not fire
        at global scope, so the duplicate could simply be inserted. Revision
        0004 fixed that, and this test went red — which was the outcome its own
        docstring predicted and asked for.

        The checker still matters, and arguably matters more now. Any database
        that reached a bad state *before* 0004 still holds those rows, and the
        0004 migration refuses to run until they are cleared. This is the tool
        that finds them, so it has to be shown working against exactly that
        state: the index is dropped for the insert, which is precisely how a
        pre-0004 database acquired its duplicates.
        """

        from app.services.memory.taxonomy import Cardinality

        shared = {
            "cardinality": Cardinality.EXCLUSIVE,
            "slot_key": "identity:global:name",
            "domain_key": "global",
        }
        first = insert_record(engine, display_text="Soham", **shared)
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "DROP INDEX IF EXISTS uq_memory_records_active_exclusive_slot"
            )
            connection.commit()
        second = insert_record(engine, display_text="Someone Else", **shared)

        report = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert report.healthy is False
        codes = {item.code for item in report.violations}
        assert "duplicate_active_exclusive_slot" in codes

        violation = next(
            item for item in report.violations if item.code == "duplicate_active_exclusive_slot"
        )
        assert set(violation.row_ids) == {first, second}

    def test_the_index_now_prevents_what_the_checker_detects(self, engine) -> None:
        """DIA-15b — preventer and detector, asserted together.

        With the index in place the second insert is refused outright, so the
        state above is unreachable through any ordinary path. Worth pinning next
        to the detector: it is the difference between a checker that finds
        historical damage and one that is the only thing standing in the way.
        """

        from sqlalchemy.exc import IntegrityError

        from app.services.memory.taxonomy import Cardinality

        shared = {
            "cardinality": Cardinality.EXCLUSIVE,
            "slot_key": "identity:global:name",
            "domain_key": "global",
        }
        insert_record(engine, display_text="Soham", **shared)
        with pytest.raises(IntegrityError):
            insert_record(engine, display_text="Someone Else", **shared)

    def test_a_violation_names_the_offending_rows(self, engine) -> None:
        """DIA-19 — a report saying only "something is wrong" is not actionable.

        An operator needs the ids to look at the rows and decide which to keep.
        """

        from app.services.memory.taxonomy import Cardinality

        shared = {"cardinality": Cardinality.EXCLUSIVE, "slot_key": "identity:global:name"}
        insert_record(engine, display_text="Soham", **shared)
        with engine.connect() as connection:
            connection.exec_driver_sql(
                "DROP INDEX IF EXISTS uq_memory_records_active_exclusive_slot"
            )
            connection.commit()
        insert_record(engine, display_text="Someone Else", **shared)
        report = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert all(item.row_ids or item.detail for item in report.violations)

    def test_an_orphaned_source_is_reported(self, engine) -> None:
        """DIA-16 — provenance pointing at a record that no longer exists.

        Like `OBX-11`, reaching this state requires switching foreign keys off,
        and for the same reason: with `PRAGMA foreign_keys=ON` the source row's
        key into `memory_records` makes the record undeletable. SQLite only
        enforces that pragma per connection and defaults to off, so a database
        touched by any process that did not set it — a migration script, a
        `sqlite3` prompt — can genuinely reach this state.

        That is precisely why the invariant check exists. It is the thing that
        finds damage the constraints were supposed to prevent, and it can only
        be trusted if it has been shown to actually fire.
        """

        from sqlalchemy import insert as sql_insert
        from sqlalchemy import text as sql_text

        from app.models.memory import MemorySource

        record_id = insert_record(engine)
        operation_id = insert_operation(engine)
        source_id = str(uuid4())
        with engine.begin() as connection:
            connection.execute(
                sql_insert(MemorySource).values(
                    id=source_id,
                    owner_id=OWNER_ID,
                    memory_id=record_id,
                    operation_id=operation_id,
                    source_kind="chat_message",
                    assertion_role="supports",
                    source_content_hash="a" * 64,
                    observed_at=FROZEN_NOW,
                )
            )

        healthy = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert healthy.healthy is True, "setup should start from a clean store"

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                sql_text("DELETE FROM memory_records WHERE id = :i"), {"i": record_id}
            )
            connection.commit()

        report = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert report.healthy is False
        violation = next(
            item for item in report.violations if item.code == "orphan_or_cross_owner_source"
        )
        assert violation.row_ids == (source_id,)

    def test_a_mismatched_owner_is_reported(self, engine) -> None:
        """DIA-17 — asking about a profile this database is not bound to."""

        report = inspect_memory_invariants(engine, owner_id=OTHER_OWNER_ID)
        assert report.healthy is False
        assert "owner_binding_mismatch" in {item.code for item in report.violations}

    def test_the_report_carries_counts_and_checksums(self, engine) -> None:
        """DIA-14b — the report is a snapshot someone can diff over time."""

        insert_record(engine)
        report = inspect_memory_invariants(engine, owner_id=OWNER_ID)
        assert report.record_counts
        assert report.schema_checksum
        assert report.canonical_data_checksum
        assert report.owner_id == OWNER_ID

    def test_healthy_is_false_whenever_anything_is_wrong(self, engine) -> None:
        """DIA-18 — one flag, so no caller has to enumerate the failure modes.

        A consumer checking `not report.violations` would miss a corrupt
        database, since integrity failures are not violations. `healthy` folds
        both.
        """

        report = inspect_memory_invariants(engine, owner_id=OTHER_OWNER_ID)
        assert report.violations
        assert report.healthy is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
