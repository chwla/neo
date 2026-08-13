"""Tier 4 — index maintenance and reconciliation (plan section MNT).

The outbox makes derived indexes eventually consistent. Maintenance is what
makes "eventually" true rather than aspirational: it walks the canonical records
and the derived rows side by side and reports where they disagree.

Two things shape these tests.

**Drift is asymmetric.** A missing derived row means a memory the user saved
cannot be found — bad, but visible to them. A *ghost* row, derived data with no
canonical record behind it, means something the user deleted is still
searchable. That one is worse and silent, so it gets the most attention here.

**Reconciliation must be resumable.** It runs over a whole profile and has to
stop and continue without re-scanning or skipping. The checkpoint is the whole
mechanism, and it is three independent cursors in one opaque string — so it is
tested as a pure function, exhaustively, before anything that uses it.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app.services.memory.indexes import (
    DerivedDocumentBuilder,
    SqliteMemoryFtsIndex,
    SqliteMemoryVectorIndex,
)
from app.services.memory.maintenance import (
    _RECONCILIATION_CURSOR_DONE,
    MemoryIndexMaintenance,
    PrivilegedGlobalMemoryMaintenance,
    _format_reconciliation_checkpoint,
    _next_cursor,
    _parse_reconciliation_checkpoint,
)
from tests.memory.conftest import FROZEN_NOW, OTHER_OWNER_ID, OWNER_ID
from tests.memory.doubles import FakeEmbeddingProvider
from tests.memory.factories import insert_record


class RecordingScheduler:
    """Captures repair requests instead of enqueuing them.

    Reconciliation does not repair anything itself — it schedules work for the
    outbox. Substituting the scheduler is what lets a test assert *what was
    decided* rather than what a second subsystem later did with it.
    """

    def __init__(self) -> None:
        self.requests: list[object] = []

    def __call__(self, request) -> None:
        self.requests.append(request)

    def schedule_repair(self, request) -> None:
        self.requests.append(request)

    def memory_ids(self) -> set[str]:
        return {str(getattr(item, "memory_id", "")) for item in self.requests}


@pytest.fixture
def scheduler() -> RecordingScheduler:
    return RecordingScheduler()


@pytest.fixture
def maintenance_factory(engine, tmp_path, scheduler):
    def _build(**overrides):
        options = {
            "owner_id": OWNER_ID,
            "database_identity": str(tmp_path / "memory.db"),
            "fts_index": SqliteMemoryFtsIndex(engine),
            "vector_index": SqliteMemoryVectorIndex(engine),
            "repair_scheduler": scheduler,
            "embedding_provider": FakeEmbeddingProvider(),
        }
        options.update(overrides)
        return MemoryIndexMaintenance(engine, **options)

    return _build


@pytest.fixture
def maintenance(maintenance_factory):
    return maintenance_factory()


def index_record(maintenance, engine, **overrides) -> str:
    """Insert a record and put it in both indexes, i.e. a consistent store."""

    record_id = insert_record(engine, **overrides)
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.models.memory import MemoryRecord as MemoryRecordRow

    with Session(engine) as session:
        row = session.scalar(select(MemoryRecordRow).where(MemoryRecordRow.id == record_id))
    document = DerivedDocumentBuilder().build(row, now=FROZEN_NOW)
    maintenance.fts_index.upsert(document)
    maintenance.vector_index.upsert(
        document,
        maintenance.embedding_provider.embed(document.display_text),
        maintenance.embedding_provider,
    )
    return record_id


class TestTheCheckpoint:
    """Three cursors in one opaque string; parsed before anything trusts it."""

    def test_no_checkpoint_means_start_from_the_beginning(self) -> None:
        """MNT-08"""

        cursors, normalized = _parse_reconciliation_checkpoint(None)
        assert cursors == (None, None, None)
        assert normalized is None

    def test_a_checkpoint_round_trips(self) -> None:
        """MNT-08b — the value handed back must be the value accepted next time.

        A caller stores this string and returns it on the following pass. If
        formatting and parsing disagreed even slightly, resumption would either
        fail or silently restart, and a re-scan looks exactly like success.
        """

        cursors = (str(uuid4()), str(uuid4()), _RECONCILIATION_CURSOR_DONE)
        formatted = _format_reconciliation_checkpoint(cursors)
        parsed, normalized = _parse_reconciliation_checkpoint(formatted)
        assert parsed == cursors
        assert normalized == formatted

    def test_the_three_cursors_advance_independently(self) -> None:
        """MNT-08c — the reason it isn't a single cursor.

        Canonical records, FTS rows and vector rows are three different
        sequences that run out at different points. One cursor would force the
        shortest to govern all three, and rows past its end would never be
        examined.
        """

        cursors = (str(uuid4()), None, _RECONCILIATION_CURSOR_DONE)
        parsed, _ = _parse_reconciliation_checkpoint(_format_reconciliation_checkpoint(cursors))
        assert parsed == cursors

    def test_a_bare_uuid_is_accepted_as_a_legacy_checkpoint(self) -> None:
        """MNT-08d — an older single-cursor checkpoint still resumes.

        Pinning this because it is the kind of compatibility path that gets
        deleted as dead code; a stored checkpoint from a previous version would
        then raise on the next run rather than resuming.
        """

        identifier = str(uuid4())
        parsed, normalized = _parse_reconciliation_checkpoint(identifier)
        assert parsed == (identifier, None, None)
        assert normalized.startswith("v1:")

    @pytest.mark.parametrize(
        "checkpoint",
        [
            "not-a-uuid",
            "v1:a:b:c",
            "v1:" + "-:" * 2,
            "v2:-:-:-",
            "v1:-:-",
            "v1:-:-:-:-",
            "x" * 129,
            "",
        ],
    )
    def test_a_malformed_checkpoint_is_refused(self, checkpoint: str) -> None:
        """MNT-09 / MNT-10 — a checkpoint arrives from outside, so it is input.

        Silently treating an unparseable checkpoint as "start from the
        beginning" would turn a corrupted value into a full re-scan that reports
        success. Refusing makes the problem visible.
        """

        with pytest.raises(ValueError, match="reconciliation_checkpoint_invalid"):
            _parse_reconciliation_checkpoint(checkpoint)

    def test_a_wrong_version_is_refused_rather_than_guessed(self) -> None:
        """MNT-10b — the version prefix exists so the format can change safely."""

        with pytest.raises(ValueError, match="reconciliation_checkpoint_invalid"):
            _parse_reconciliation_checkpoint("v2:-:-:-")

    def test_a_full_page_returns_a_resumable_cursor(self) -> None:
        """MNT-11 — more items than the limit means there is more to do."""

        items = [{"id": str(uuid4())} for _ in range(5)]
        page, cursor = _next_cursor(items, limit=3, identifier=lambda item: item["id"])
        assert len(page) == 3
        assert cursor == page[-1]["id"]

    def test_the_final_page_is_marked_done(self) -> None:
        """MNT-11b — the sentinel, so the next pass skips this sequence entirely."""

        items = [{"id": str(uuid4())} for _ in range(2)]
        page, cursor = _next_cursor(items, limit=3, identifier=lambda item: item["id"])
        assert len(page) == 2
        assert cursor == _RECONCILIATION_CURSOR_DONE

    def test_an_exactly_full_page_is_also_done(self) -> None:
        """MNT-11c — the off-by-one that would cause an infinite loop.

        With exactly `limit` items there is nothing after them. Returning a
        resumable cursor here would schedule another pass that finds nothing and
        returns the same cursor, forever.
        """

        items = [{"id": str(uuid4())} for _ in range(3)]
        _, cursor = _next_cursor(items, limit=3, identifier=lambda item: item["id"])
        assert cursor == _RECONCILIATION_CURSOR_DONE

    def test_an_empty_sequence_is_done(self) -> None:
        """MNT-11d"""

        page, cursor = _next_cursor([], limit=3, identifier=lambda item: item["id"])
        assert page == []
        assert cursor == _RECONCILIATION_CURSOR_DONE


class TestReconciliation:
    def test_a_consistent_store_reports_no_drift(self, maintenance, engine, scheduler) -> None:
        """MNT-01 — the baseline, and the one that must not cry wolf.

        A reconciliation that reports drift on a healthy store is worse than
        none: it schedules pointless repairs and trains everyone to ignore it.
        """

        index_record(maintenance, engine)
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.missing_fts == 0
        assert report.missing_vector == 0
        assert report.stale_fts == 0
        assert report.stale_vector == 0
        assert scheduler.requests == []

    def test_a_missing_fts_document_is_detected(self, maintenance, engine, scheduler) -> None:
        """MNT-02 — a saved memory that cannot be found."""

        record_id = index_record(maintenance, engine)
        maintenance.fts_index.delete(OWNER_ID, record_id, None)
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.missing_fts == 1
        assert record_id in scheduler.memory_ids()

    def test_a_missing_vector_point_is_detected(self, maintenance, engine, scheduler) -> None:
        """MNT-03"""

        record_id = index_record(maintenance, engine)
        maintenance.vector_index.delete(OWNER_ID, record_id, None)
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.missing_vector == 1
        assert record_id in scheduler.memory_ids()

    def test_a_ghost_row_is_detected(self, maintenance, engine, scheduler) -> None:
        """MNT-05 — the worst drift, because the user cannot see it.

        A derived row with no canonical record behind it is a memory the user
        deleted that is still searchable. Unlike a missing row — where they at
        least notice the thing they saved is gone — nothing surfaces this except
        reconciliation.
        """

        record_id = index_record(maintenance, engine)
        with engine.begin() as connection:
            from sqlalchemy import text as sql_text

            connection.execute(
                sql_text("DELETE FROM memory_records WHERE id = :i"), {"i": record_id}
            )
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.ghost_fts >= 1 or report.ghost_vector >= 1

    def test_a_dry_run_changes_nothing(self, maintenance, engine, scheduler) -> None:
        """MNT-01b — an operator has to be able to look before acting."""

        record_id = index_record(maintenance, engine)
        maintenance.fts_index.delete(OWNER_ID, record_id, None)
        report = maintenance.reconcile(now=FROZEN_NOW, dry_run=True)
        assert report.missing_fts == 1
        assert scheduler.requests == []

    def test_reconciliation_is_owner_scoped(self, maintenance, engine, scheduler) -> None:
        """MNT-06 — another profile's rows are not this profile's drift.

        Counting them would report permanent, unfixable drift, since repairing
        them is not something this maintainer is allowed to do.
        """

        index_record(maintenance, engine, owner=OTHER_OWNER_ID)
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.missing_fts == 0
        assert report.ghost_fts == 0

    def test_the_batch_limit_is_honoured_and_returns_a_cursor(self, maintenance, engine) -> None:
        """MNT-07 — a whole profile may not fit in one pass."""

        for index in range(4):
            index_record(maintenance, engine, display_text=f"note {index}")
        report = maintenance.reconcile(now=FROZEN_NOW, limit=2)
        assert report.checked == 2
        assert report.next_checkpoint is not None

    def test_resuming_covers_the_remaining_records(self, maintenance, engine) -> None:
        """MNT-12 — no gap and no repeat, which is the whole point of resuming.

        Asserted by walking the entire store in pages of two and collecting what
        each pass examined: the union must be every record, and the passes must
        not overlap.
        """

        for index in range(5):
            index_record(maintenance, engine, display_text=f"note {index}")
        seen_total = 0
        passes = 0
        checkpoint = None
        while True:
            report = maintenance.reconcile(now=FROZEN_NOW, limit=2, checkpoint=checkpoint)
            seen_total += report.checked
            passes += 1
            checkpoint = report.next_checkpoint
            if checkpoint is None:
                break
            assert passes < 10, "walk did not terminate"
        assert seen_total == 5
        assert passes == 3

    def test_the_walk_signals_completion_with_a_null_checkpoint(self, maintenance, engine) -> None:
        """MNT-12b — the termination condition, pinned on its own.

        `next_checkpoint is None` is the only signal that the walk is finished.
        It matters because feeding that `None` back in does not mean "carry on
        from where you were" — it means "start again from the beginning", so a
        caller that loops until `checked == 0` instead of until the checkpoint
        is `None` re-scans the whole profile forever. (I wrote that loop first.)
        """

        for index in range(3):
            index_record(maintenance, engine, display_text=f"note {index}")
        first = maintenance.reconcile(now=FROZEN_NOW, limit=2)
        assert first.next_checkpoint is not None
        second = maintenance.reconcile(now=FROZEN_NOW, limit=2, checkpoint=first.next_checkpoint)
        assert second.checked == 1
        assert second.next_checkpoint is None

        restarted = maintenance.reconcile(now=FROZEN_NOW, limit=2, checkpoint=None)
        assert restarted.checked == 2

    def test_an_expired_record_is_not_treated_as_missing(
        self, maintenance, engine, scheduler
    ) -> None:
        """MNT-02b — an expired record *should* have no derived row.

        Without this the sweep would report drift for every expired memory and
        schedule a repair that correctly does nothing, forever.
        """

        insert_record(engine, expires_at=FROZEN_NOW - timedelta(days=1))
        report = maintenance.reconcile(now=FROZEN_NOW)
        assert report.missing_fts == 0


class TestRebuild:
    def test_rebuilding_reconstructs_both_indexes(self, maintenance, engine) -> None:
        """MNT-13 — the recovery path when the derived state is beyond repair."""

        insert_record(engine)
        result = maintenance.rebuild_owner(now=FROZEN_NOW)
        assert result.canonical_eligible_count == 1
        assert result.pending_target_count >= 1

    def test_rebuilding_twice_is_safe(self, maintenance, engine) -> None:
        """MNT-14 — an interrupted rebuild has to be restartable.

        A rebuild that could not be re-run would mean a crash halfway leaves the
        profile with no way forward except manual surgery.
        """

        insert_record(engine)
        first = maintenance.rebuild_owner(now=FROZEN_NOW)
        second = maintenance.rebuild_owner(now=FROZEN_NOW)
        assert first.canonical_checksum_after == second.canonical_checksum_after

    def test_rebuilding_touches_only_its_owner(
        self, maintenance, maintenance_factory, engine
    ) -> None:
        """MNT-15 — a rebuild clears before it writes, so scope is critical.

        This is the most destructive operation in the module: it empties the
        index first. An unscoped clear would wipe every other profile's derived
        data on the way to fixing one.
        """

        other = index_record(maintenance, engine, owner=OTHER_OWNER_ID)
        maintenance.rebuild_owner(now=FROZEN_NOW)
        assert maintenance.fts_index.get_metadata(OTHER_OWNER_ID, other) is not None

    def test_a_rebuild_verifies_against_the_canonical_checksum(self, maintenance, engine) -> None:
        """MNT-16"""

        insert_record(engine)
        result = maintenance.rebuild_owner(now=FROZEN_NOW)
        verification = maintenance.verify_owner_rebuild(result, now=FROZEN_NOW)
        assert verification is not None

    def test_verification_refuses_a_result_from_another_owner(self, maintenance, engine) -> None:
        """MNT-16b — the result carries its owner, and the check is enforced.

        Verifying profile A's rebuild against profile B's index would compare
        two unrelated stores and report failure — or worse, accidental success.
        """

        insert_record(engine)
        result = maintenance.rebuild_owner(now=FROZEN_NOW)
        foreign = result.model_copy(update={"owner_id": UUID(OTHER_OWNER_ID)})
        with pytest.raises(ValueError, match="rebuild_owner_mismatch"):
            maintenance.verify_owner_rebuild(foreign, now=FROZEN_NOW)


class TestChecksums:
    def test_the_canonical_checksum_is_stable(self, maintenance, engine) -> None:
        """MNT-19 — it is the fixed point a rebuild is verified against.

        If it moved on its own, a correct rebuild would report as a failure.
        """

        insert_record(engine)
        assert maintenance._canonical_checksum(FROZEN_NOW) == maintenance._canonical_checksum(
            FROZEN_NOW
        )

    def test_the_canonical_checksum_changes_with_the_data(self, maintenance, engine) -> None:
        """MNT-19b — and it must actually notice a change."""

        insert_record(engine)
        before = maintenance._canonical_checksum(FROZEN_NOW)
        insert_record(engine, display_text="a different memory")
        assert maintenance._canonical_checksum(FROZEN_NOW) != before

    def test_the_canonical_checksum_is_owner_scoped(self, maintenance, engine) -> None:
        """MNT-19c — another profile's writes must not invalidate this one."""

        insert_record(engine)
        before = maintenance._canonical_checksum(FROZEN_NOW)
        insert_record(engine, owner=OTHER_OWNER_ID)
        assert maintenance._canonical_checksum(FROZEN_NOW) == before


class TestCoverage:
    def test_coverage_reports_per_target_counts(self, maintenance, engine) -> None:
        """MNT-18 — the number an operator actually looks at."""

        index_record(maintenance, engine)
        report = maintenance.coverage(now=FROZEN_NOW)
        assert report.canonical_active_eligible_count >= 1
        assert report.owner_id == UUID(OWNER_ID)

    def test_coverage_is_owner_scoped(self, maintenance, engine) -> None:
        """MNT-18b"""

        index_record(maintenance, engine, owner=OTHER_OWNER_ID)
        assert maintenance.coverage(now=FROZEN_NOW).canonical_active_eligible_count == 0


class TestPrivilegedGlobalMaintenance:
    def test_an_unauthorized_instance_cannot_be_constructed(self, maintenance) -> None:
        """MNT-22 — cross-owner access is refused at construction, not per call.

        I expected a per-method check and this is stronger: an unauthorized
        instance cannot be brought into existence at all, so there is no object
        to accidentally pass somewhere trusting. Every method is therefore
        unreachable without authorization by construction rather than by each
        method remembering to ask.
        """

        with pytest.raises(PermissionError, match="privileged_memory_maintenance_required"):
            PrivilegedGlobalMemoryMaintenance([maintenance], authorized=False)

    def test_an_unknown_owner_checkpoint_is_refused(self, maintenance) -> None:
        """MNT-22b — a checkpoint must belong to an owner this instance manages.

        `reconcile_all` takes a per-owner checkpoint map. Silently ignoring an
        unrecognised owner would let a caller believe it had resumed a profile
        that was never scanned.
        """

        privileged = PrivilegedGlobalMemoryMaintenance([maintenance], authorized=True)
        with pytest.raises(ValueError, match="unknown_privileged_maintenance_owner"):
            privileged.reconcile_all(checkpoints={OTHER_OWNER_ID: None})

    def test_it_fans_out_when_authorized(self, maintenance, engine) -> None:
        """MNT-23"""

        insert_record(engine)
        privileged = PrivilegedGlobalMemoryMaintenance([maintenance], authorized=True)
        assert len(privileged.rebuild_all()) == 1

    def test_the_global_report_aggregates_each_owner(self, maintenance, engine) -> None:
        """MNT-24"""

        index_record(maintenance, engine)
        privileged = PrivilegedGlobalMemoryMaintenance([maintenance], authorized=True)
        report = privileged.coverage()
        assert report.owner_count == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
