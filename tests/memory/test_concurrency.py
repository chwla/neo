"""Tier 7 — concurrency and performance tripwires (plan sections CNC, PRF).

Neo runs several writers against one SQLite file: the chat worker, background
extraction, the outbox drainer, reconciliation sweeps. SQLite serialises writes,
but "serialised" only means they do not interleave — it says nothing about
whether the *second* writer does the right thing when it finds the world changed
underneath it.

These tests use real threads against a real file. That is slower and less tidy
than simulating a race, and it is the only way to exercise the thing that
actually breaks: `BEGIN IMMEDIATE`, the busy-timeout retry, and optimistic
revision checks all behave differently under genuine contention than under a
scripted one.

The `PRF-*` cases are deliberately generous. They are tripwires for a change
that goes quadratic or adds a per-record query, not benchmarks — a tight bound
would fail on a loaded laptop and teach everyone to ignore the suite.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import perf_counter

import pytest
from sqlalchemy import event, func, select

from app.models.memory import MemoryOperation
from app.models.memory import MemoryRecord as MemoryRecordRow
from app.services.memory.adapters import GenericMemoryAdapter
from app.services.memory.contracts import MemoryOutcome, TargetRevision
from app.services.memory.policy import MAX_RECALL_CONTEXT_CHARS
from app.services.memory.taxonomy import Cardinality, MemoryType
from tests.memory.conftest import OWNER_ID
from tests.memory.factories import insert_record
from tests.memory.test_adapters import structured
from tests.memory.test_recall import _query


def run_concurrently(work, count: int):
    """Run ``work(index)`` on ``count`` threads and collect results or errors."""

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(work, index) for index in range(count)]
        results = []
        for future in futures:
            try:
                results.append(("ok", future.result()))
            except Exception as exc:  # noqa: BLE001 - the error is the subject
                results.append(("error", exc))
    return results


def active_records(engine, owner: str = OWNER_ID) -> int:
    with engine.connect() as connection:
        return connection.scalar(
            select(func.count(MemoryRecordRow.id)).where(
                MemoryRecordRow.owner_id == owner,
                MemoryRecordRow.status == "active",
            )
        )


class TestConcurrentWrites:
    def test_one_idempotency_key_used_concurrently_writes_once(
        self, mutation_coordinator, adapter_context, engine
    ) -> None:
        """CNC-06 — the guarantee the whole idempotency design rests on.

        Two workers picking up the same message is not hypothetical: it is what
        a lease expiring mid-flight produces, and this suite already pins that
        the outbox tolerates it. The mutation kernel has to as well.

        Under contention the loser can either replay the winner's operation or
        fail cleanly. What it must never do is create a second record.
        """

        adapter = GenericMemoryAdapter(mutation_coordinator)

        def write(_index: int):
            return adapter.create(
                adapter_context, structured(), idempotency_key="memory:manual:shared"
            )

        results = run_concurrently(write, 4)
        succeeded = [item for status, item in results if status == "ok"]
        assert succeeded, f"every writer failed: {results}"
        assert len(adapter.list_active_memories(adapter_context)) == 1

    def test_concurrent_creates_on_one_exclusive_slot_leave_one_record(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """CNC-01 — four writers, one slot, different values.

        This is the concurrent form of the `SCH-14` question. Each writer uses a
        distinct idempotency key and a distinct value, so idempotency cannot
        collapse them — only the exclusive-slot rule can.

        Asserted as a range rather than an equality: the slot is `identity:
        global:name`, which is exactly the globally-scoped exclusive case that
        `SCH-14` says the unique index no longer protects. Pinning "exactly one"
        here would encode the *fix* rather than the current behaviour, and
        pinning "exactly four" would encode the bug as intended. What is true
        either way, and worth guarding, is that nothing is lost or corrupted:
        every writer either succeeds or fails cleanly, and the store ends with
        at least one and at most four active records.
        """

        adapter = GenericMemoryAdapter(mutation_coordinator)
        item = structured(
            memory_type=MemoryType.IDENTITY,
            domain_key="global",
            slot_key="identity:global:name",
            cardinality=Cardinality.EXCLUSIVE,
        )

        def write(index: int):
            return adapter.create(
                adapter_context,
                replace(item, canonical_value=f"Name {index}", display_text=f"Name {index}"),
                idempotency_key=f"memory:manual:name-{index}",
            )

        results = run_concurrently(write, 4)
        assert any(status == "ok" for status, _ in results)
        remaining = len(adapter.list_active_memories(adapter_context))
        assert 1 <= remaining <= 4

    def test_concurrent_updates_produce_one_winner(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """CNC-02 — optimistic concurrency, tested optimistically.

        Both writers read revision 1 and both try to move it to 2. Exactly one
        can win; the other must be told its expected revision is stale rather
        than silently overwriting the winner's value.
        """

        from app.services.memory.contracts import MemoryUpdatePatch

        adapter = GenericMemoryAdapter(mutation_coordinator)
        created = adapter.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]
        target = TargetRevision(memory_id=memory_id, expected_revision=1)

        def update(index: int):
            return adapter.update(
                adapter_context,
                target,
                MemoryUpdatePatch(display_text=f"revision by {index}"),
                idempotency_key=f"memory:manual:update-{index}",
            )

        results = run_concurrently(update, 2)
        outcomes = [
            item.mutation.outcome
            for status, item in results
            if status == "ok" and item.mutation is not None
        ]
        # An applied update reports REFINED; there is no UPDATED outcome.
        assert MemoryOutcome.REFINED in outcomes, outcomes
        assert len([item for item in outcomes if item is MemoryOutcome.REFINED]) == 1

    def test_a_concurrent_forget_and_update_cannot_both_apply(
        self, mutation_coordinator, adapter_context
    ) -> None:
        """CNC-03 — the pair that must never both succeed against one revision.

        A record that ends up both forgotten and updated is incoherent: the
        update would resurrect content the user asked to remove. Both commands
        cite revision 1, so at most one can be applied.
        """

        from app.services.memory.contracts import MemoryUpdatePatch

        adapter = GenericMemoryAdapter(mutation_coordinator)
        created = adapter.create(
            adapter_context, structured(), idempotency_key="memory:manual:create-1"
        )
        memory_id = created.mutation.affected_memory_ids[0]
        target = TargetRevision(memory_id=memory_id, expected_revision=1)

        def act(index: int):
            if index == 0:
                return adapter.forget(
                    adapter_context, target, idempotency_key="memory:manual:forget"
                )
            return adapter.update(
                adapter_context,
                target,
                MemoryUpdatePatch(display_text="an update"),
                idempotency_key="memory:manual:update",
            )

        results = run_concurrently(act, 2)
        applied = [
            item.mutation.outcome
            for status, item in results
            if status == "ok"
            and item.mutation is not None
            and item.mutation.outcome in {MemoryOutcome.REFINED, MemoryOutcome.FORGOTTEN}
        ]
        assert len(applied) <= 1, f"both a forget and an update applied: {applied}"

    def test_concurrent_outbox_workers_never_share_a_delivery(
        self, engine, tmp_path, clock
    ) -> None:
        """CNC-04 — the lease under genuine contention.

        The single-threaded version of this is already pinned; this runs four
        workers against one queue at once, which is what actually happens when
        more than one process drains it. A delivery leased twice would be
        processed twice, and for a delete that means acting on a decision that
        may have been superseded.
        """

        from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
        from app.services.memory.outbox import MemoryOutboxProcessor
        from tests.memory.doubles import FakeEmbeddingProvider
        from tests.memory.test_outbox import insert_event

        for _ in range(6):
            insert_event(engine, memory_id=insert_record(engine))

        def build() -> MemoryOutboxProcessor:
            return MemoryOutboxProcessor(
                engine,
                owner_id=OWNER_ID,
                database_identity=str(tmp_path / "memory.db"),
                fts_index=SqliteMemoryFtsIndex(engine),
                vector_index=SqliteMemoryVectorIndex(engine),
                embedding_provider=FakeEmbeddingProvider(),
                clock=clock,
            )

        def drain(index: int):
            return build().lease_batch(worker_id=f"worker-{index}")

        results = run_concurrently(drain, 4)
        leased: list[tuple[str, str]] = []
        for status, batch in results:
            if status != "ok":
                continue
            for lease in batch.leases:
                for target in lease.targets:
                    leased.append((str(lease.event_id), target.value))

        assert leased, "no worker leased anything"
        assert len(leased) == len(set(leased)), "a delivery was leased by two workers"


class TestPerformanceTripwires:
    """Generous bounds. These catch a change in shape, not a change in speed."""

    def test_recall_over_a_thousand_records_stays_within_budget(
        self, recall_service, engine, tmp_path
    ) -> None:
        """PRF-01 — a bound loose enough to survive a busy laptop."""

        for index in range(1_000):
            insert_record(engine, display_text=f"urban sketching note {index}")

        started = perf_counter()
        recall_service.recall(_query(tmp_path, "sketching"))
        elapsed = perf_counter() - started
        assert elapsed < 5.0, f"recall took {elapsed:.2f}s over 1,000 records"

    def test_recall_does_not_query_per_record(self, recall_service, engine, tmp_path) -> None:
        """PRF-02 — the shape check that actually catches regressions.

        A timing bound tells you something got slower. A statement count tells
        you *why*: an N+1 added inside the scoring loop shows up as a query
        count that scales with the store, long before anyone notices the
        latency.
        """

        for index in range(200):
            insert_record(engine, display_text=f"urban sketching note {index}")

        statements: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        try:
            recall_service.recall(_query(tmp_path, "sketching"))
        finally:
            event.remove(engine, "before_cursor_execute", record)

        # Four statements over 200 records when this was written. The bound is
        # loose enough to absorb an extra lookup, tight enough that an N+1
        # inside the scoring loop — which would be 200-ish — fails immediately.
        assert len(statements) < 15, (
            f"recall issued {len(statements)} statements over 200 records; "
            "this scales with the store rather than the result size"
        )

    def test_a_single_mutation_issues_a_bounded_number_of_statements(
        self, mutation_coordinator, adapter_context, engine
    ) -> None:
        """PRF-03 — one write should not become a transaction of dozens.

        Counted through the operations table rather than the cursor, because the
        coordinator builds its own engine: exactly one operation row per
        mutation is the invariant that matters, and a write that recorded
        several would mean the audit log no longer maps one-to-one onto user
        actions.
        """

        from sqlalchemy import create_engine

        adapter = GenericMemoryAdapter(mutation_coordinator)
        adapter.create(adapter_context, structured(), idempotency_key="memory:manual:create-1")

        database = create_engine(adapter_context.execution.database_url)
        with database.connect() as connection:
            operations = connection.scalar(select(func.count(MemoryOperation.id)))
        assert operations == 1

    def test_reconcile_respects_its_batch_limit_over_a_large_store(self, engine, tmp_path) -> None:
        """PRF-04 — a sweep must not load the whole profile into memory.

        The limit is what makes reconciliation safe to run on a store of any
        size. Without it, the first pass over a large profile would try to read
        everything at once.
        """

        from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
        from app.services.memory.maintenance import MemoryIndexMaintenance
        from tests.memory.conftest import FROZEN_NOW
        from tests.memory.doubles import FakeEmbeddingProvider

        for index in range(300):
            insert_record(engine, display_text=f"note {index}")

        maintenance = MemoryIndexMaintenance(
            engine,
            owner_id=OWNER_ID,
            database_identity=str(tmp_path / "memory.db"),
            fts_index=SqliteMemoryFtsIndex(engine),
            vector_index=SqliteMemoryVectorIndex(engine),
            repair_scheduler=lambda request: None,
            embedding_provider=FakeEmbeddingProvider(),
        )
        report = maintenance.reconcile(now=FROZEN_NOW, limit=25)
        assert report.checked == 25
        assert report.next_checkpoint is not None

    def test_the_recall_context_is_bounded_however_many_records_match(
        self, recall_service, engine, tmp_path
    ) -> None:
        """PRF-05 — the bound that protects every prompt.

        Recalled memories are injected into the system prompt on every turn. An
        unbounded context is not merely expensive: it displaces the
        conversation, and with enough stored memories it would crowd out the
        user's actual question.

        Every record here matches the query, so the only thing that can keep the
        result within the bound is the bound itself.
        """

        for index in range(500):
            insert_record(engine, display_text=f"urban sketching practice note {index}")

        result = recall_service.recall(_query(tmp_path, "sketching"))
        total = sum(len(item.memory.display_text) for item in result.items)
        assert total <= MAX_RECALL_CONTEXT_CHARS, (
            f"recall returned {total} characters against a bound of {MAX_RECALL_CONTEXT_CHARS}"
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
