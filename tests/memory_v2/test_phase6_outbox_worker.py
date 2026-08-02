from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from app.models.memory_v2 import (
    MemoryDerivedStateV2,
    MemoryOutboxDeliveryV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemoryVectorPointV2,
)
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.phase6_contracts import (
    DerivedTarget,
    IndexRepairRequest,
    RetryPolicy,
)
from app.services.memory_v2.queries import RecallQuery
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.taxonomy import MemoryType
from tests.memory_v2.phase5_helpers import add_memory, query_context
from tests.memory_v2.phase6_helpers import phase6_harness, phase6_services


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class CountingFts:
    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.upsert_calls = 0

    def upsert(self, document):
        self.upsert_calls += 1
        return self.delegate.upsert(document)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class FailingFts:
    def upsert(self, _document):
        raise RuntimeError("fts fixture unavailable")

    def get_metadata(self, *_args):
        return None


def _create(tmp_path):
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="phase6-worker",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short cinematic reels",
    )
    return harness, memory_id


def test_canonical_commit_only_queues_outbox_and_worker_processes_post_commit(tmp_path) -> None:
    harness, memory_id = _create(tmp_path)
    services = phase6_services(harness)
    try:
        row = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        event = services.phase5.session.scalar(
            select(MemoryOutboxV2).where(MemoryOutboxV2.memory_id == str(memory_id))
        )
        assert row is not None and row.status == "active"
        assert event is not None and event.state == "pending"
        assert services.provider_source.calls == 0

        batch = services.processor.lease_batch(worker_id="worker-a")
        assert len(batch.leases) == 1
        results = services.processor.process_batch(batch)
        assert results[0].canonical_mutations == 0
        assert {item.target: item.operation for item in results[0].diagnostics} == {
            DerivedTarget.FTS: "upsert",
            DerivedTarget.VECTOR: "upsert",
        }
        assert services.provider_source.calls == 1
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).revision == 1
        assert services.fts.get_metadata(str(row.owner_id), str(memory_id)) is not None
        assert services.vector.get_metadata(str(row.owner_id), str(memory_id)) is not None
    finally:
        services.close()


def test_pending_survives_restart_and_active_lease_is_exclusive(tmp_path) -> None:
    harness, _memory_id = _create(tmp_path)
    services = phase6_services(harness)
    clock = MutableClock()
    try:
        first = MemoryV2OutboxProcessor(
            services.phase5.session.get_bind(),
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            fts_index=services.fts,
            vector_index=services.vector,
            embedding_provider=services.provider,
            clock=clock,
        )
        lease = first.lease_batch(worker_id="crashed-worker", lease_seconds=10)
        assert len(lease.leases) == 1
        restarted = MemoryV2OutboxProcessor(
            services.phase5.session.get_bind(),
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            fts_index=services.fts,
            vector_index=services.vector,
            embedding_provider=services.provider,
            clock=clock,
        )
        assert not restarted.lease_batch(worker_id="worker-b").leases
        clock.now += timedelta(seconds=11)
        recovered = restarted.lease_batch(worker_id="worker-b")
        assert len(recovered.leases) == 1
        restarted.process_batch(recovered)
    finally:
        services.close()


def test_fts_success_vector_failure_is_independent_and_retryable(tmp_path) -> None:
    harness, memory_id = _create(tmp_path)
    services = phase6_services(harness)
    clock = MutableClock()
    fts = CountingFts(services.fts)
    services.processor = MemoryV2OutboxProcessor(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=fts,
        vector_index=services.vector,
        embedding_provider=services.provider,
        clock=clock,
    )
    try:
        services.provider_source.fail = True
        first = services.processor.lease_batch(worker_id="worker-a")
        result = services.processor.process_batch(first)[0]
        assert result.completed_targets == (DerivedTarget.FTS,)
        assert result.retryable_targets == (DerivedTarget.VECTOR,)
        assert services.fts.get_metadata(services.phase5.repository.owner_id, str(memory_id))
        assert (
            services.vector.get_metadata(services.phase5.repository.owner_id, str(memory_id))
            is None
        )
        deliveries = list(
            services.phase5.session.scalars(
                select(MemoryOutboxDeliveryV2).order_by(MemoryOutboxDeliveryV2.target)
            )
        )
        assert {item.target: item.state for item in deliveries} == {
            "fts": "current",
            "vector": "failed",
        }
        derived = {
            item.target: item.state
            for item in services.phase5.session.scalars(select(MemoryDerivedStateV2))
        }
        assert derived == {"fts": "current", "vector": "failed"}
        services.phase5.session.rollback()
        services.provider_source.fail = False
        clock.now += timedelta(seconds=10)
        retry = services.processor.lease_batch(worker_id="worker-b")
        assert retry.leases[0].targets == (DerivedTarget.VECTOR,)
        services.processor.process_batch(retry)
        assert services.vector.get_metadata(services.phase5.repository.owner_id, str(memory_id))
        assert fts.upsert_calls == 1
        lexical = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            fts_index=services.fts,
            semantic_provider=services.provider,
            vector_index=services.vector,
        ).recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="short cinematic reels",
            )
        )
        assert lexical.canonical_ids == (memory_id,)
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).revision == 1
    finally:
        services.close()


def test_all_derived_failures_leave_canonical_committed_and_outbox_retryable(
    tmp_path,
) -> None:
    harness, memory_id = _create(tmp_path)
    services = phase6_services(harness)
    clock = MutableClock()
    processor = MemoryV2OutboxProcessor(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=FailingFts(),
        vector_index=services.vector,
        embedding_provider=services.provider,
        clock=clock,
    )
    services.provider_source.fail = True
    try:
        batch = processor.lease_batch(worker_id="all-fail-worker")
        result = processor.process_batch(batch)[0]
        assert set(result.retryable_targets) == {
            DerivedTarget.FTS,
            DerivedTarget.VECTOR,
        }
        assert all(item.failure_code is not None for item in result.diagnostics)
        assert all("cinematic" not in item.model_dump_json() for item in result.diagnostics)
        services.phase5.session.expire_all()
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        event = services.phase5.session.scalar(
            select(MemoryOutboxV2).where(MemoryOutboxV2.memory_id == str(memory_id))
        )
        assert record.status == "active" and record.revision == 1
        assert event.state == "failed"
        assert event.next_retry_at is not None
        assert event.last_error == "derived_target_failed"
    finally:
        services.close()


def test_vector_upsert_crash_retry_is_idempotent(tmp_path) -> None:
    harness, _memory_id = _create(tmp_path)
    services = phase6_services(harness)
    clock = MutableClock()
    crashed = False

    def crash(target, _event_id):
        nonlocal crashed
        if target is DerivedTarget.VECTOR and not crashed:
            crashed = True
            raise SystemExit("simulated process loss")

    processor = MemoryV2OutboxProcessor(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=services.fts,
        vector_index=services.vector,
        embedding_provider=services.provider,
        clock=clock,
        after_target_write=crash,
    )
    try:
        batch = processor.lease_batch(worker_id="worker-a", lease_seconds=5)
        try:
            processor.process_batch(batch)
        except SystemExit:
            pass
        clock.now += timedelta(seconds=6)
        processor.after_target_write = None
        retry = processor.lease_batch(worker_id="worker-b")
        processor.process_batch(retry)
        count = services.phase5.session.scalar(select(func.count(MemoryVectorPointV2.id)))
        services.phase5.session.rollback()
        assert count == 1
    finally:
        services.close()


def test_permanent_failure_dead_letters_and_explicit_requeue_recovers(tmp_path) -> None:
    harness, _memory_id = _create(tmp_path)
    services = phase6_services(
        harness,
        retry_policy=RetryPolicy(maximum_attempts=5, dead_letter_threshold=1),
    )
    try:
        services.provider_source.fail = True
        batch = services.processor.lease_batch(worker_id="worker-a")
        result = services.processor.process_batch(batch)[0]
        assert result.dead_lettered_targets == (DerivedTarget.VECTOR,)
        assert services.processor.requeue_dead_letter(
            batch.leases[0].event_id, DerivedTarget.VECTOR
        )
        assert not services.processor.requeue_dead_letter(
            batch.leases[0].event_id, DerivedTarget.VECTOR
        )
        services.provider_source.fail = False
        retry = services.processor.lease_batch(worker_id="worker-b")
        services.processor.process_batch(retry)
        states = list(services.phase5.session.scalars(select(MemoryDerivedStateV2.state)))
        services.phase5.session.rollback()
        assert "current" in states
    finally:
        services.close()


def test_old_canonical_remove_event_cannot_delete_newer_derived_revision(tmp_path) -> None:
    harness, memory_id = _create(tmp_path)
    services = phase6_services(harness)
    try:
        services.processor.process_batch(services.processor.lease_batch(worker_id="initial-worker"))
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        record.revision = 2
        services.phase5.session.add(
            MemoryOutboxV2(
                id=str(uuid4()),
                owner_id=record.owner_id,
                event_kind="canonical_upsert",
                memory_id=record.id,
                canonical_revision=2,
                content_hash=record.canonical_fingerprint,
                event_payload_json={},
                state="pending",
                attempts=0,
                event_idempotency_key="phase6:newer-upsert",
            )
        )
        services.phase5.session.commit()
        services.processor.process_batch(services.processor.lease_batch(worker_id="newer-worker"))
        newer = services.vector.get_metadata(record.owner_id, record.id)
        assert newer is not None and newer["canonical_revision"] == 2

        services.phase5.session.add(
            MemoryOutboxV2(
                id=str(uuid4()),
                owner_id=record.owner_id,
                event_kind="canonical_remove",
                memory_id=record.id,
                canonical_revision=1,
                content_hash=record.canonical_fingerprint,
                event_payload_json={},
                state="pending",
                attempts=0,
                event_idempotency_key="phase6:old-remove",
            )
        )
        services.phase5.session.commit()
        services.processor.process_batch(services.processor.lease_batch(worker_id="remove-worker"))
        preserved = services.vector.get_metadata(record.owner_id, record.id)
        assert preserved is not None
        assert preserved["canonical_revision"] == 2
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, record.id).revision == 2
    finally:
        services.close()


def test_vector_delete_runs_without_embedding_provider(tmp_path) -> None:
    harness, memory_id = _create(tmp_path)
    services = phase6_services(harness)
    try:
        services.processor.process_batch(
            services.processor.lease_batch(worker_id="initial-vector-worker")
        )
        owner_id = services.phase5.repository.owner_id
        metadata = services.vector.get_metadata(owner_id, str(memory_id))
        provider_calls = services.provider_source.calls
        delete_only = MemoryV2OutboxProcessor(
            services.phase5.session.get_bind(),
            owner_id=owner_id,
            database_identity=services.phase5.repository.database_identity,
            vector_index=services.vector,
            embedding_provider=None,
        )
        delete_only.schedule_repair(
            IndexRepairRequest(
                owner_id=owner_id,
                memory_id=memory_id,
                action="delete",
                target=DerivedTarget.VECTOR,
                reason="provider_independent_delete_fixture",
                expected_hash=metadata["content_hash"],
            )
        )
        result = delete_only.process_batch(
            delete_only.lease_batch(worker_id="provider-free-delete-worker")
        )[0]
        assert result.completed_targets == (DerivedTarget.VECTOR,)
        assert result.diagnostics[0].operation == "delete"
        assert result.diagnostics[0].repair_reason == "provider_independent_delete_fixture"
        assert services.vector.get_metadata(owner_id, str(memory_id)) is None
        assert services.provider_source.calls == provider_calls
    finally:
        services.close()
