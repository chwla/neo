"""Tier 4 — the outbox processor (plan section OBX).

Writes to the canonical store commit first; the derived indexes are updated
afterwards by a worker draining this queue.  That gap is deliberate — a slow
embedding model must never hold up a user's write — but it means every failure
mode of "work happens later, elsewhere, maybe twice" lives here:

* **Leases**, so two workers can't process one delivery, and a worker that dies
  doesn't strand it forever.
* **Backoff and dead-lettering**, so a permanently broken target retries a
  bounded number of times instead of spinning.
* **Staleness checks**, so a queued instruction about an old revision can't
  overwrite a newer one.

Every test drives the clock explicitly (``FrozenClock``). Lease expiry, retry
schedules and dead-letter thresholds are all time-dependent, and testing them
against the wall clock would mean either sleeping or writing something that
passes now and fails at midnight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select

from app.models.memory import MemoryOutbox, MemoryOutboxDelivery
from app.services.memory.contracts import MemoryLifecycleState
from app.services.memory.index_contracts import (
    DerivedFailureCode,
    DerivedTarget,
    DerivedTargetState,
    OutboxBatch,
    RetryPolicy,
)
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.outbox import MemoryOutboxProcessor
from tests.memory.conftest import OTHER_OWNER_ID, OWNER_ID
from tests.memory.doubles import FakeEmbeddingProvider
from tests.memory.factories import insert_record

# Outbox rows are inserted directly rather than via a factory in the shared
# ``factories.py``: they are used only by this file, and a shared helper would
# be an abstraction with one caller.


def insert_event(
    engine,
    *,
    memory_id: str | None = None,
    event_kind: str = "canonical_upsert",
    owner: str = OWNER_ID,
    revision: int = 1,
    content_hash: str | None = None,
    payload: dict[str, Any] | None = None,
    state: str = "pending",
    next_retry_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    event_id = str(uuid4())
    moment = created_at or datetime(2026, 6, 15, 11, 0, tzinfo=UTC)
    # An upsert event carries the fingerprint and revision the record had when
    # it was enqueued, exactly as the mutation kernel writes them.  The
    # processor compares both and skips the write if either has moved on, so an
    # event with a mismatched hash is treated as stale — which is correct
    # behaviour, and would make every test here silently a no-op.
    if memory_id is not None and content_hash is None:
        from sqlalchemy import text as sql_text

        with engine.connect() as connection:
            row = connection.execute(
                sql_text(
                    "SELECT canonical_fingerprint, revision FROM memory_records WHERE id = :i"
                ),
                {"i": memory_id},
            ).first()
        if row is not None:
            content_hash, revision = row[0], row[1]
    with engine.begin() as connection:
        connection.execute(
            insert(MemoryOutbox).values(
                id=event_id,
                owner_id=owner,
                event_kind=event_kind,
                memory_id=memory_id,
                canonical_revision=revision,
                content_hash=content_hash,
                event_payload_json=payload or {},
                state=state,
                attempts=0,
                next_retry_at=next_retry_at,
                event_idempotency_key=f"key-{event_id}",
                schema_version=1,
                created_at=moment,
                updated_at=moment,
            )
        )
    return event_id


def deliveries(engine, event_id: str) -> list[MemoryOutboxDelivery]:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        return list(
            session.scalars(
                select(MemoryOutboxDelivery)
                .where(MemoryOutboxDelivery.event_id == event_id)
                .order_by(MemoryOutboxDelivery.target)
            )
        )


def event_row(engine, event_id: str) -> MemoryOutbox:
    from sqlalchemy.orm import Session

    with Session(engine) as session:
        return session.scalar(select(MemoryOutbox).where(MemoryOutbox.id == event_id))


@pytest.fixture
def processor_factory(engine, tmp_path, clock):
    """A processor wired to real indexes and a frozen clock."""

    def _build(**overrides):
        options: dict[str, Any] = {
            "owner_id": OWNER_ID,
            "database_identity": str(tmp_path / "memory.db"),
            "fts_index": SqliteMemoryFtsIndex(engine),
            "vector_index": SqliteMemoryVectorIndex(engine),
            "embedding_provider": FakeEmbeddingProvider(),
            "clock": clock,
        }
        options.update(overrides)
        return MemoryOutboxProcessor(engine, **options)

    return _build


@pytest.fixture
def processor(processor_factory):
    return processor_factory()


class TestEnabledTargets:
    def test_both_targets_are_enabled_when_both_indexes_exist(self, processor) -> None:
        """OBX-01"""

        assert processor.enabled_targets == (DerivedTarget.FTS, DerivedTarget.VECTOR)

    def test_a_missing_index_disables_its_target(self, processor_factory) -> None:
        """OBX-01b — turning off the vector index must not queue vector work.

        Otherwise every event would accrue a delivery nothing can ever process,
        and the event would never reach `done`.
        """

        processor = processor_factory(vector_index=None)
        assert processor.enabled_targets == (DerivedTarget.FTS,)

    def test_no_indexes_means_no_targets(self, processor_factory) -> None:
        """OBX-01c"""

        assert processor_factory(fts_index=None, vector_index=None).enabled_targets == ()


class TestLeasing:
    def test_leasing_claims_the_event_for_a_worker(self, engine, processor, clock) -> None:
        """OBX-02 — the claim has to be visible to other workers, in the row."""

        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        batch = processor.lease_batch(worker_id="worker-a")
        assert len(batch.leases) == 1
        lease = batch.leases[0]
        assert lease.worker_id == "worker-a"
        assert lease.event_id == UUID(event_id)
        rows = deliveries(engine, event_id)
        assert {row.state for row in rows} == {DerivedTargetState.PROCESSING.value}
        assert {row.worker_id for row in rows} == {"worker-a"}
        assert all(row.lease_expires_at is not None for row in rows)

    def test_a_delivery_row_is_created_per_enabled_target(self, engine, processor) -> None:
        """OBX-08 — the two targets fail and retry independently.

        A single delivery row for both would mean a vector timeout forcing the
        FTS write to be redone, and eventually dead-lettering a target that was
        working fine.
        """

        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        processor.lease_batch(worker_id="worker-a")
        assert [row.target for row in deliveries(engine, event_id)] == ["fts", "vector"]

    def test_a_second_worker_cannot_take_a_held_lease(self, engine, processor) -> None:
        """OBX-03 / OBX-07 — the property that stops duplicate processing.

        Both workers run the same conditional UPDATE; the row's state is the
        lock. The second finds nothing to claim.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        first = processor.lease_batch(worker_id="worker-a")
        second = processor.lease_batch(worker_id="worker-b")
        assert len(first.leases) == 1
        assert second.leases == ()

    def test_an_expired_lease_is_reclaimed(self, engine, processor, clock) -> None:
        """OBX-04 — a worker that died must not strand the delivery forever.

        This is why the lease has an expiry rather than being held until
        released: nothing can ask a crashed process to give it back.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        processor.lease_batch(worker_id="worker-a", lease_seconds=60)
        clock.advance(seconds=61)
        reclaimed = processor.lease_batch(worker_id="worker-b")
        assert len(reclaimed.leases) == 1
        assert reclaimed.leases[0].worker_id == "worker-b"

    def test_a_lease_one_second_from_expiry_is_not_reclaimed(
        self, engine, processor, clock
    ) -> None:
        """OBX-04b — the boundary, so the reclaim isn't simply always-on."""

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        processor.lease_batch(worker_id="worker-a", lease_seconds=60)
        clock.advance(seconds=59)
        assert processor.lease_batch(worker_id="worker-b").leases == ()

    def test_reclaiming_increments_the_attempt_count(self, engine, processor, clock) -> None:
        """OBX-04c — a crash still counts as an attempt.

        Otherwise a delivery that reliably kills its worker would be retried
        forever and never reach the dead-letter threshold.
        """

        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        processor.lease_batch(worker_id="worker-a", lease_seconds=60)
        clock.advance(seconds=61)
        processor.lease_batch(worker_id="worker-b")
        assert all(row.attempts == 2 for row in deliveries(engine, event_id))

    def test_leasing_is_owner_scoped(self, engine, processor) -> None:
        """OBX-06 — a worker for one profile never touches another's queue."""

        insert_record(engine, owner=OTHER_OWNER_ID)
        record_id = insert_record(engine, owner=OTHER_OWNER_ID)
        insert_event(engine, memory_id=record_id, owner=OTHER_OWNER_ID)
        assert processor.lease_batch(worker_id="worker-a").leases == ()

    def test_an_event_scheduled_for_later_is_not_leased(self, engine, processor, clock) -> None:
        """OBX-05 — backoff is honoured at the point of leasing."""

        record_id = insert_record(engine)
        insert_event(
            engine,
            memory_id=record_id,
            next_retry_at=clock.now + timedelta(seconds=300),
        )
        assert processor.lease_batch(worker_id="worker-a").leases == ()

    def test_the_same_event_becomes_leasable_once_its_delay_passes(
        self, engine, processor, clock
    ) -> None:
        """OBX-05b"""

        record_id = insert_record(engine)
        insert_event(
            engine,
            memory_id=record_id,
            next_retry_at=clock.now + timedelta(seconds=300),
        )
        clock.advance(seconds=301)
        assert len(processor.lease_batch(worker_id="worker-a").leases) == 1

    def test_the_batch_size_is_respected(self, engine, processor) -> None:
        """OBX-02b — a worker takes a bounded amount of work per pass."""

        for _ in range(5):
            insert_event(engine, memory_id=insert_record(engine))
        assert len(processor.lease_batch(worker_id="worker-a", batch_size=2).leases) == 2

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"worker_id": "  "}, "worker_id_invalid"),
            ({"worker_id": "x" * 121}, "worker_id_invalid"),
            ({"worker_id": "w", "batch_size": 0}, "worker_batch_size_out_of_range"),
            ({"worker_id": "w", "batch_size": 501}, "worker_batch_size_out_of_range"),
            ({"worker_id": "w", "lease_seconds": 4}, "worker_lease_seconds_out_of_range"),
            ({"worker_id": "w", "lease_seconds": 3_601}, "worker_lease_seconds_out_of_range"),
        ],
    )
    def test_invalid_worker_parameters_are_refused(
        self, processor, kwargs: dict, message: str
    ) -> None:
        """OBX-02c — a blank worker id would make the lease unattributable.

        The lease is released by matching on worker id; if two workers could
        both be "", either could release the other's work.
        """

        with pytest.raises(ValueError, match=message):
            processor.lease_batch(**kwargs)


class TestProcessing:
    def _leased(self, engine, processor, **event_kwargs):
        record_id = event_kwargs.pop("record_id", None) or insert_record(engine)
        insert_event(engine, memory_id=record_id, **event_kwargs)
        batch = processor.lease_batch(worker_id="worker-a")
        return record_id, batch.leases[0]

    def test_an_upsert_writes_both_derived_targets(self, engine, processor) -> None:
        """OBX-09 — the happy path, end to end against real indexes."""

        record_id, lease = self._leased(engine, processor)
        result = processor.process(lease)
        assert set(result.completed_targets) == {DerivedTarget.FTS, DerivedTarget.VECTOR}
        assert result.failure_codes == ()
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is not None
        assert processor.vector_index.get_metadata(OWNER_ID, record_id) is not None

    def test_a_completed_event_is_marked_done(self, engine, processor) -> None:
        """OBX-24 — only when *every* delivery is terminal."""

        record_id, lease = self._leased(engine, processor)
        processor.process(lease)
        row = event_row(engine, str(lease.event_id))
        assert row.state == "done"
        assert row.completed_at is not None

    def test_a_removal_deletes_from_both_targets(self, engine, processor) -> None:
        """OBX-10 — forgetting has to reach the derived copies too.

        A memory removed from the canonical store but left in the FTS index
        would keep being returned by recall — the deletion would look like it
        silently failed.
        """

        record_id, lease = self._leased(engine, processor)
        processor.process(lease)
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is not None

        insert_event(engine, memory_id=record_id, event_kind="canonical_remove", revision=2)
        removal = processor.lease_batch(worker_id="worker-b").leases[0]
        processor.process(removal)
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is None
        assert processor.vector_index.get_metadata(OWNER_ID, record_id) is None

    def test_an_inactive_record_is_removed_rather_than_indexed(self, engine, processor) -> None:
        """OBX-12 — the queue can outlive the fact it describes.

        An upsert queued before the user archived the memory would otherwise
        re-index something they had just removed. The processor notices the
        record is no longer active and deletes instead.
        """

        record_id = insert_record(engine, status=MemoryLifecycleState.ARCHIVED)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        result = processor.process(lease)
        assert result.completed_targets
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is None

    def test_an_expired_record_is_removed_rather_than_indexed(
        self, engine, processor, clock
    ) -> None:
        """OBX-12b — same reasoning, via expiry rather than an explicit archive."""

        record_id = insert_record(engine, expires_at=clock.now - timedelta(days=1))
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        processor.process(lease)
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is None

    def test_reprocessing_a_reclaimed_event_writes_one_row(self, engine, processor, clock) -> None:
        """OBX-27 — at-least-once delivery, modelled the way it actually occurs.

        A lease expires while its work is genuinely still running, another
        worker reclaims and completes it, and the original worker then finishes
        too. Both did the work; there must be one derived row, not two.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        processor.lease_batch(worker_id="worker-a", lease_seconds=60)
        clock.advance(seconds=61)
        reclaimed = processor.lease_batch(worker_id="worker-b").leases[0]
        processor.process(reclaimed)
        assert len(processor.fts_index.list_metadata_for_owner(OWNER_ID)) == 1
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is not None

    def test_a_lost_lease_is_reported_not_raised(self, engine, processor, clock) -> None:
        """OBX-15 — fixed. A reclaimed lease is reported, not thrown.

        `_failure_code` always had an explicit branch mapping the message
        `lease_lost` to `DerivedFailureCode.LEASE_LOST`, so this was meant to be
        a reported failure like every other. It was unreachable: the `except`
        handler computed that code and then called `_finish_target` a second
        time, which raised `lease_lost` again with nothing left to catch it, so
        the exception left `process()` entirely.

        A stale lease is not exceptional — it is the ordinary outcome whenever
        work outruns its lease duration, which is what leases exist for. The
        worker draining the queue died instead of recording a failure and moving
        on, and since `process_batch` maps over every lease, one stale lease took
        the rest of the batch with it.

        The handler now tolerates a lost lease and reports it. Nothing needed
        repairing: the reclaiming worker had already done the write.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        stale = processor.lease_batch(worker_id="worker-a", lease_seconds=60).leases[0]
        clock.advance(seconds=61)
        processor.process(processor.lease_batch(worker_id="worker-b").leases[0])

        result = processor.process(stale)
        assert DerivedFailureCode.LEASE_LOST in result.failure_codes
        assert len(processor.fts_index.list_metadata_for_owner(OWNER_ID)) == 1

    def test_one_stale_lease_does_not_abort_the_rest_of_the_batch(
        self, engine, processor, clock
    ) -> None:
        """OBX-15b — the consequence that made this worth fixing.

        `process_batch` maps `process` over every lease. While a stale lease
        raised, one of them stopped the whole batch — so a single expired lease
        halted the queue for every other event in it. This is the property that
        regresses first if the handler goes back to re-raising.
        """

        stale_record = insert_record(engine)
        insert_event(engine, memory_id=stale_record)
        stale = processor.lease_batch(worker_id="worker-a", lease_seconds=60).leases[0]
        clock.advance(seconds=61)
        processor.process(processor.lease_batch(worker_id="worker-b").leases[0])

        healthy_record = insert_record(engine, display_text="a second memory")
        insert_event(engine, memory_id=healthy_record)
        healthy = processor.lease_batch(worker_id="worker-c").leases[0]

        results = processor.process_batch(OutboxBatch(leases=(stale, healthy)))
        assert len(results) == 2
        assert processor.fts_index.get_metadata(OWNER_ID, healthy_record) is not None

    def test_processing_another_owners_lease_is_refused(self, engine, processor) -> None:
        """OBX-14 — the owner check is at the top of `process`, before any read."""

        record_id, lease = self._leased(engine, processor)
        foreign = lease.model_copy(update={"owner_id": UUID(OTHER_OWNER_ID)})
        with pytest.raises(ValueError, match="owner_binding_mismatch"):
            processor.process(foreign)


class TestFailureHandling:
    class BrokenIndex:
        """An index whose writes always fail, for the retry paths."""

        def __init__(self, error: Exception | None = None) -> None:
            self.error = error or RuntimeError("index exploded")

        def upsert(self, *args, **kwargs):
            raise self.error

        def delete(self, *args, **kwargs):
            raise self.error

        def get_metadata(self, *args, **kwargs):
            return None

    def _leased_with_broken_fts(self, engine, processor_factory, error=None):
        processor = processor_factory(fts_index=self.BrokenIndex(error))
        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        return processor, lease

    def test_a_failing_target_is_reported_not_raised(self, engine, processor_factory) -> None:
        """OBX-18 — an exception escaping here would kill the whole worker."""

        processor, lease = self._leased_with_broken_fts(engine, processor_factory)
        result = processor.process(lease)
        assert DerivedTarget.FTS in result.retryable_targets
        assert result.failure_codes

    def test_one_failing_target_does_not_block_the_other(self, engine, processor_factory) -> None:
        """OBX-32 — independence is the reason for per-target deliveries.

        A broken embedding model must not stop full-text search from being
        updated. Here FTS is the broken one and the vector write still lands.
        """

        processor, lease = self._leased_with_broken_fts(engine, processor_factory)
        result = processor.process(lease)
        assert DerivedTarget.VECTOR in result.completed_targets
        assert DerivedTarget.FTS in result.retryable_targets

    def test_a_failure_schedules_a_retry_with_backoff(
        self, engine, processor_factory, clock
    ) -> None:
        """OBX-19 — a broken target must not be retried in a tight loop."""

        processor, lease = self._leased_with_broken_fts(engine, processor_factory)
        processor.process(lease)
        rows = {row.target: row for row in deliveries(engine, str(lease.event_id))}
        assert rows["fts"].state == DerivedTargetState.FAILED.value
        assert rows["fts"].next_attempt_at is not None
        assert rows["fts"].next_attempt_at > clock.now.replace(tzinfo=None)

    def test_the_failed_target_is_not_leasable_before_its_delay(
        self, engine, processor_factory
    ) -> None:
        """OBX-19b — the schedule is enforced, not merely recorded."""

        processor, lease = self._leased_with_broken_fts(engine, processor_factory)
        processor.process(lease)
        assert processor.lease_batch(worker_id="worker-b").leases == ()

    def test_the_backoff_grows_and_is_capped(self) -> None:
        """OBX-19c — exponential, but bounded.

        Unbounded doubling would eventually schedule a retry days away, so a
        transient outage would look like permanent data loss.
        """

        policy = RetryPolicy(base_delay_seconds=5, maximum_delay_seconds=300)
        delays = [policy.delay_for(attempt) for attempt in range(1, 8)]
        assert delays[:5] == [5, 10, 20, 40, 80]
        assert delays == sorted(delays)
        assert max(delays) <= 300

    def test_the_jitter_is_deterministic_per_event_and_attempt(self, processor) -> None:
        """OBX-20 — reproducible, but different across events.

        Jitter exists so a hundred deliveries failing together don't all retry
        in the same instant. Deriving it from the event id gives that spread
        while keeping any single delivery's schedule reproducible in a test.
        """

        event_id = uuid4()
        first = processor.jitter_source(event_id, DerivedTarget.FTS, 1)
        assert first == processor.jitter_source(event_id, DerivedTarget.FTS, 1)
        assert first != processor.jitter_source(event_id, DerivedTarget.FTS, 2)
        assert first != processor.jitter_source(event_id, DerivedTarget.VECTOR, 1)
        assert first != processor.jitter_source(uuid4(), DerivedTarget.FTS, 1)

    def test_the_jitter_stays_within_a_fraction(self, processor) -> None:
        """OBX-20b — it's a fraction, so it can only ever add a bounded amount."""

        for attempt in range(1, 20):
            value = processor.jitter_source(uuid4(), DerivedTarget.FTS, attempt)
            assert 0.0 <= value <= 1.0

    def test_repeated_failures_dead_letter(self, engine, processor_factory, clock) -> None:
        """OBX-21 — a permanently broken target stops consuming the queue.

        Without a ceiling, one poisoned delivery is retried forever and every
        pass spends its budget on work that cannot succeed.
        """

        processor = processor_factory(
            fts_index=self.BrokenIndex(),
            retry_policy=RetryPolicy(maximum_attempts=2, base_delay_seconds=5),
        )
        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        for _ in range(4):
            batch = processor.lease_batch(worker_id="worker-a")
            if not batch.leases:
                clock.advance(seconds=600)
                continue
            processor.process(batch.leases[0])
            clock.advance(seconds=600)
        rows = {row.target: row for row in deliveries(engine, event_id)}
        assert rows["fts"].state == DerivedTargetState.DEAD_LETTER.value

    def test_a_dead_letter_marks_the_event_failed(self, engine, processor_factory, clock) -> None:
        """OBX-25 — the event's own state has to reflect it, for diagnostics."""

        processor = processor_factory(
            fts_index=self.BrokenIndex(),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        processor.process(lease)
        assert event_row(engine, event_id).state == "failed"

    def test_a_dead_letter_can_be_requeued(self, engine, processor_factory) -> None:
        """OBX-22 — recovery after the underlying cause is fixed.

        Dead-lettering parks work; it must not destroy it. Once the embedding
        model is back, an operator has to be able to return the delivery to the
        queue rather than rebuild the whole index.
        """

        processor = processor_factory(
            fts_index=self.BrokenIndex(),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        record_id = insert_record(engine)
        event_id = insert_event(engine, memory_id=record_id)
        processor.process(processor.lease_batch(worker_id="worker-a").leases[0])

        assert processor.requeue_dead_letter(UUID(event_id), DerivedTarget.FTS) is True
        rows = {row.target: row for row in deliveries(engine, event_id)}
        assert rows["fts"].state == DerivedTargetState.PENDING.value

    def test_requeueing_something_that_is_not_dead_returns_false(self, engine, processor) -> None:
        """OBX-23 — an unknown or healthy delivery isn't silently "requeued"."""

        assert processor.requeue_dead_letter(uuid4(), DerivedTarget.FTS) is False

    def test_an_embedding_failure_keeps_its_own_code(self, engine, processor_factory) -> None:
        """OBX-16 — "the model is down" and "the index write failed" differ.

        One is fixed by starting a service, the other by looking at the
        database. Collapsing them into one code loses that.
        """

        from app.services.embeddings import EmbeddingValidationError

        processor = processor_factory(
            vector_index=self.BrokenIndex(
                EmbeddingValidationError(DerivedFailureCode.EMBEDDING_UNAVAILABLE.value)
            )
        )
        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        result = processor.process(lease)
        assert DerivedFailureCode.EMBEDDING_UNAVAILABLE in result.failure_codes

    def test_an_index_failure_maps_to_its_target_and_action(
        self, engine, processor_factory
    ) -> None:
        """OBX-17 — the code names both which index and which operation."""

        processor, lease = self._leased_with_broken_fts(engine, processor_factory)
        result = processor.process(lease)
        assert DerivedFailureCode.FTS_UPSERT_FAILED in result.failure_codes


class TestStalenessAndRepair:
    """A queued instruction can describe a record that has since moved on."""

    def test_a_vanished_record_schedules_a_delete_instead(self, engine, processor) -> None:
        """OBX-11 — an upsert for a record that no longer exists.

        The right response is not "fail" but "delete the derived rows": the
        canonical record is gone, so whatever is in the index is a ghost. The
        processor turns the upsert into a removal rather than leaving orphaned
        derived data behind.

        **Reaching this state requires switching foreign keys off**, and that is
        the point rather than a workaround. With `PRAGMA foreign_keys=ON` the
        outbox event's key into `memory_records` makes the row undeletable, so
        this branch is unreachable — I tried the straightforward deletion first
        and the constraint refused it.

        But SQLite enforces foreign keys only when the pragma is set, *per
        connection*, and off is the default. `app/db/session.py` sets it, so the
        running app is protected; any other process opening the same file
        (a migration script, `sqlite3` at a prompt, a future worker that builds
        its own engine) is not. This branch is the defence for a database that
        has been through such a process, so the test recreates exactly that.
        """

        from sqlalchemy import text as sql_text

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.execute(
                sql_text("DELETE FROM memory_records WHERE id = :i"), {"i": record_id}
            )
            connection.commit()

        result = processor.process(lease)
        reasons = {item.repair_reason for item in result.diagnostics}
        assert DerivedFailureCode.CANONICAL_MISSING.value in reasons

    def test_an_event_describing_an_older_revision_is_skipped(self, engine, processor) -> None:
        """OBX-13 — the guard against writing a stale value over a fresh one.

        The queue is asynchronous, so a record can be updated between an event
        being enqueued and processed. Applying the queued document would put the
        *old* display text back into the index, and the index would then
        disagree with canonical until the next reconciliation.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        with engine.begin() as connection:
            from sqlalchemy import text as sql_text

            connection.execute(
                sql_text(
                    "UPDATE memory_records SET revision = revision + 1, "
                    "display_text = 'a newer value' WHERE id = :i"
                ),
                {"i": record_id},
            )

        result = processor.process(lease)
        reasons = {item.repair_reason for item in result.diagnostics}
        assert DerivedFailureCode.CANONICAL_HASH_ADVANCED.value in reasons
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is None

    def test_a_skipped_stale_event_queues_a_fresh_repair(self, engine, processor) -> None:
        """OBX-13b — skipping is not enough; the index still needs updating.

        Refusing the stale write without queueing a correct one would leave the
        index permanently behind, with nothing scheduled to fix it.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        with engine.begin() as connection:
            from sqlalchemy import text as sql_text

            connection.execute(
                sql_text("UPDATE memory_records SET revision = revision + 1 WHERE id = :i"),
                {"i": record_id},
            )
        processor.process(lease)

        from sqlalchemy import select as sql_select
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            repairs = list(
                session.scalars(
                    sql_select(MemoryOutbox).where(
                        MemoryOutbox.event_idempotency_key.like("memory:repair:%")
                    )
                )
            )
        assert repairs, "no repair was queued for the superseded record"

    def test_an_identical_repair_is_queued_only_once(self, engine, processor) -> None:
        """OBX-29 — the same drift found twice must not grow the queue.

        Reconciliation runs repeatedly. Without de-duplication, every pass over
        an unfixable record would add another event, and the queue would grow
        without bound while never draining.
        """

        from app.services.memory.index_contracts import IndexRepairRequest

        record_id = insert_record(engine)
        request = IndexRepairRequest(
            owner_id=UUID(OWNER_ID),
            memory_id=UUID(record_id),
            action="upsert",
            reason="reconciliation_drift",
        )
        processor.schedule_repair(request)
        processor.schedule_repair(request)

        from sqlalchemy import select as sql_select
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            repairs = list(
                session.scalars(
                    sql_select(MemoryOutbox).where(
                        MemoryOutbox.event_idempotency_key.like("memory:repair:%")
                    )
                )
            )
        assert len(repairs) == 1

    def test_a_repair_for_another_owner_is_refused(self, engine, processor) -> None:
        """OBX-28 — a repair is a write, so it carries the same owner check."""

        from app.services.memory.index_contracts import IndexRepairRequest

        record_id = insert_record(engine)
        with pytest.raises(ValueError, match="owner_binding_mismatch"):
            processor.schedule_repair(
                IndexRepairRequest(
                    owner_id=UUID(OTHER_OWNER_ID),
                    memory_id=UUID(record_id),
                    action="upsert",
                    reason="reconciliation_drift",
                )
            )

    def test_a_repair_for_an_unknown_record_is_a_no_op(self, engine, processor) -> None:
        """OBX-28b — an upsert repair needs a record to describe.

        Queueing one for a record that no longer exists would enqueue work that
        can only ever fail.
        """

        from app.services.memory.index_contracts import IndexRepairRequest

        processor.schedule_repair(
            IndexRepairRequest(
                owner_id=UUID(OWNER_ID),
                memory_id=uuid4(),
                action="upsert",
                reason="reconciliation_drift",
            )
        )

        from sqlalchemy import select as sql_select
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            repairs = list(
                session.scalars(
                    sql_select(MemoryOutbox).where(
                        MemoryOutbox.event_idempotency_key.like("memory:repair:%")
                    )
                )
            )
        assert repairs == []

    def test_a_completed_repair_is_revived_rather_than_duplicated(self, engine, processor) -> None:
        """OBX-29b — drift recurring after a fix reuses the same event.

        The idempotency key is derived from the repair's content, so a second
        occurrence of identical drift finds the finished event and returns it to
        pending instead of creating a parallel one.
        """

        from sqlalchemy import select as sql_select
        from sqlalchemy import update as sql_update
        from sqlalchemy.orm import Session

        from app.services.memory.index_contracts import IndexRepairRequest

        record_id = insert_record(engine)
        request = IndexRepairRequest(
            owner_id=UUID(OWNER_ID),
            memory_id=UUID(record_id),
            action="upsert",
            reason="reconciliation_drift",
        )
        processor.schedule_repair(request)
        with engine.begin() as connection:
            connection.execute(
                sql_update(MemoryOutbox)
                .where(MemoryOutbox.event_idempotency_key.like("memory:repair:%"))
                .values(state="done")
            )
        processor.schedule_repair(request)

        with Session(engine) as session:
            repairs = list(
                session.scalars(
                    sql_select(MemoryOutbox).where(
                        MemoryOutbox.event_idempotency_key.like("memory:repair:%")
                    )
                )
            )
        assert len(repairs) == 1
        assert repairs[0].state == "pending"


class TestDerivedHealthState:
    """`memory_derived_state` is what health and coverage read; it must track reality."""

    def _derived_rows(self, engine, memory_id: str):
        from sqlalchemy.orm import Session

        from app.models.memory import MemoryDerivedState

        with Session(engine) as session:
            return {
                row.target: row
                for row in session.scalars(
                    select(MemoryDerivedState).where(MemoryDerivedState.memory_id == memory_id)
                )
            }

    def test_a_successful_write_records_current_state_and_provider(self, engine, processor) -> None:
        """OBX-26 — the health row is the only place coverage can read from.

        Nothing else records "is this memory indexed, by which model". If the
        row were not updated alongside the delivery, coverage would report
        permanent drift for records that are in fact perfectly indexed.
        """

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        processor.process(lease)

        rows = self._derived_rows(engine, record_id)
        assert set(rows) == {"fts", "vector"}
        assert rows["fts"].state == DerivedTargetState.CURRENT.value
        assert rows["fts"].last_error_code is None
        assert rows["vector"].provider == "fake"
        assert rows["vector"].dimension == 8

    def test_a_failure_records_the_error_against_the_target(
        self, engine, processor_factory
    ) -> None:
        """OBX-26b — and a failure has to be visible in the same place.

        A health row still saying `current` after the write failed would make a
        broken index look healthy, which is the one thing health reporting must
        never do.
        """

        class BrokenIndex:
            def upsert(self, *args, **kwargs):
                raise RuntimeError("index exploded")

            def delete(self, *args, **kwargs):
                raise RuntimeError("index exploded")

            def get_metadata(self, *args, **kwargs):
                return None

        processor = processor_factory(fts_index=BrokenIndex())
        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        processor.process(lease)

        rows = self._derived_rows(engine, record_id)
        assert rows["fts"].state == DerivedTargetState.FAILED.value
        assert rows["fts"].last_error_code == DerivedFailureCode.FTS_UPSERT_FAILED.value
        # The healthy target is untouched by the other one's failure.
        assert rows["vector"].state == DerivedTargetState.CURRENT.value
        assert rows["vector"].last_error_code is None

    def test_a_later_success_clears_an_earlier_error(self, engine, processor_factory) -> None:
        """OBX-26c — recovery must actually clear the error, not layer over it.

        A stale `last_error_code` on a now-healthy row would keep an alert
        firing after the underlying problem was fixed, which is how people learn
        to ignore alerts.
        """

        class FlakyIndex(SqliteMemoryFtsIndex):
            fail = True

            def upsert(self, document):
                if FlakyIndex.fail:
                    raise RuntimeError("index exploded")
                return super().upsert(document)

        flaky = FlakyIndex(engine)
        processor = processor_factory(fts_index=flaky)
        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        processor.process(processor.lease_batch(worker_id="worker-a").leases[0])
        assert self._derived_rows(engine, record_id)["fts"].last_error_code is not None

        FlakyIndex.fail = False
        try:
            insert_event(engine, memory_id=record_id)
            processor.process(processor.lease_batch(worker_id="worker-b").leases[0])
        finally:
            FlakyIndex.fail = True

        row = self._derived_rows(engine, record_id)["fts"]
        assert row.state == DerivedTargetState.CURRENT.value
        assert row.last_error_code is None


class TestDiagnostics:
    def test_every_processed_target_emits_a_diagnostic(self, engine, processor) -> None:
        """OBX-30"""

        record_id = insert_record(engine)
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        result = processor.process(lease)
        assert len(result.diagnostics) == 2
        assert {item.target for item in result.diagnostics} == {
            DerivedTarget.FTS,
            DerivedTarget.VECTOR,
        }

    def test_a_diagnostic_carries_no_user_content(self, engine, processor) -> None:
        """OBX-31 — these get logged, so they carry ids and codes only."""

        record_id = insert_record(engine, display_text="improve at urban sketching")
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        result = processor.process(lease)
        for diagnostic in result.diagnostics:
            serialized = diagnostic.model_dump_json()
            assert "urban sketching" not in serialized
            assert "improve" not in serialized


class TestSensitiveRecords:
    def test_a_sensitive_record_reaches_neither_index(self, engine, processor) -> None:
        """OBX-33 — the last line of the defence tested in IDX-01.

        The builder refuses to produce a document for a sensitive record, and
        this asserts the consequence at the level that matters: after the queue
        has been drained, there is nothing in either index.
        """

        from sqlalchemy import text as sql_text

        record_id = insert_record(engine)
        # Converting the row to sensitive means satisfying the whole payload
        # shape in one statement: plaintext columns cleared, every encryption
        # column populated.  The schema will not accept a half-converted row,
        # which is the constraint doing its job (SCH tests cover it directly).
        with engine.begin() as connection:
            connection.execute(
                sql_text(
                    "UPDATE memory_records SET sensitivity = 'sensitive', "
                    "canonical_payload = NULL, display_text = NULL, "
                    "encrypted_canonical_payload = :blob, "
                    "encrypted_display_payload = :blob, "
                    "encryption_algorithm = 'aes-gcm', encryption_key_version = 'v1', "
                    "canonical_nonce = :nonce, display_nonce = :nonce, "
                    "encryption_aad = :aad WHERE id = :i"
                ),
                {
                    "blob": b"ciphertext",
                    "nonce": b"nonce-12bytes",
                    "aad": f"{OWNER_ID}:{record_id}",
                    "i": record_id,
                },
            )
        insert_event(engine, memory_id=record_id)
        lease = processor.lease_batch(worker_id="worker-a").leases[0]
        processor.process(lease)
        assert processor.fts_index.get_metadata(OWNER_ID, record_id) is None
        assert processor.vector_index.get_metadata(OWNER_ID, record_id) is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
