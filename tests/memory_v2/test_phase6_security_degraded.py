from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread

from sqlalchemy import func, select

import app.repositories.memory_store as memory_store_module
from app.core.config import Settings
from app.models.memory_v2 import MemoryDerivedStateV2, MemoryOutboxV2, MemoryRecordV2
from app.repositories.memory_store import MemoryStore
from app.services.embeddings import ValidatedMemoryV2EmbeddingProvider
from app.services.memory_v2.adapters import ImportMemoryV2Adapter, StructuredMemoryInput
from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.maintenance import MemoryV2IndexMaintenance
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.queries import RecallMode, RecallQuery
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.runtime import build_phase6_recall_dependencies
from app.services.memory_v2.taxonomy import Cardinality, MemoryType
from tests.memory_v2.phase5_helpers import add_memory, query_context
from tests.memory_v2.phase6_helpers import phase6_harness, phase6_services


class ForbiddenVector:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, *_args):
        self.calls += 1
        raise AssertionError("vector search must not run")


class CountingFts:
    def __init__(self) -> None:
        self.calls = 0

    def search(self, *_args):
        self.calls += 1
        return []


class BlockingEmbeddingSource:
    provider_name = "blocking-fixture"
    model_name = "blocking-fixture-model"

    def __init__(self, started: Event, release: Event) -> None:
        self.started = started
        self.release = release

    def embed(self, _text):
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("fixture_release_timeout")
        return [0.1, 0.2, 0.3]


def test_sensitive_is_not_embedded_and_prohibited_creates_no_derived_work(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    sensitive_id = add_memory(
        adapter,
        harness,
        key="phase6-sensitive",
        memory_type=MemoryType.KNOWLEDGE,
        domain="health_fitness",
        slot="knowledge:health_fitness:sensitive",
        text="synthetic sensitive fixture value",
        sensitivity=Sensitivity.SENSITIVE,
    )
    rejected = adapter.create(
        harness.context,
        StructuredMemoryInput(
            memory_type=MemoryType.KNOWLEDGE,
            domain_key="global",
            slot_key="knowledge:global:prohibited",
            cardinality=Cardinality.ADDITIVE,
            canonical_value="my password is prohibited-fixture-value",
            display_text="my password is prohibited-fixture-value",
        ),
        idempotency_key="phase6:prohibited",
    )
    assert rejected.mutation is not None and not rejected.mutation.active_memory_ids
    services = phase6_services(harness)
    try:
        batch = services.processor.lease_batch(worker_id="sensitive-worker")
        services.processor.process_batch(batch)
        assert services.provider_source.calls == 0
        states = list(
            services.phase5.session.scalars(
                select(MemoryDerivedStateV2).where(
                    MemoryDerivedStateV2.memory_id == str(sensitive_id)
                )
            )
        )
        assert {item.state for item in states} == {"not_applicable"}
        prohibited_events = services.phase5.session.scalar(
            select(func.count(MemoryOutboxV2.id)).where(
                MemoryOutboxV2.event_idempotency_key == "phase6:prohibited"
            )
        )
        assert prohibited_events == 0
    finally:
        services.close()


def test_incognito_disabled_and_semantic_flag_off_make_zero_phase6_calls(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    add_memory(
        adapter,
        harness,
        key="phase6-gates",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short cinematic reels",
    )
    services = phase6_services(harness)
    vector = ForbiddenVector()
    fts = CountingFts()
    repairs = []
    metrics = []
    try:
        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            semantic_provider=services.provider,
            vector_index=vector,
            fts_index=fts,
            repair_scheduler=repairs.append,
            metric_recorder=metrics.append,
        )
        for context in (
            query_context(services.phase5, incognito=True),
            query_context(services.phase5, memory_enabled=False),
            query_context(services.phase5, mode=RecallMode.DETERMINISTIC),
            query_context(services.phase5, mode=RecallMode.BROAD),
        ):
            query = (
                RecallQuery(context=context, memory_type=MemoryType.GOAL)
                if context.mode is RecallMode.DETERMINISTIC
                else RecallQuery(
                    context=context,
                    text=(
                        "show my saved memories"
                        if context.mode is RecallMode.BROAD
                        else "short cinematic reels"
                    ),
                )
            )
            recall.recall(query)
        assert services.provider_source.calls == 0
        assert vector.calls == 0
        assert fts.calls == 0
        assert repairs == []
        assert metrics == []
        recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="reels",
            )
        )
        assert services.provider_source.calls == 0
        assert vector.calls == 0
        disabled_flags = replace(
            services.phase5.harness.coordinator.flags,
            semantic_recall_enabled=False,
        )
        CanonicalRecallService(
            services.phase5.repository,
            flags=disabled_flags,
            semantic_provider=services.provider,
            vector_index=vector,
            fts_index=fts,
        ).recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="short cinematic reels",
            )
        )
        assert services.provider_source.calls == 0
        assert vector.calls == 0
    finally:
        services.close()


def test_phase6_dependencies_fail_closed_for_owner_outside_enabled_cohort(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    add_memory(
        adapter,
        harness,
        key="phase6-owner-cohort-gate",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:owner-cohort-gate",
        text="synthetic owner cohort gate fixture",
    )
    services = phase6_services(harness)
    try:
        flags = replace(
            services.phase5.harness.coordinator.flags,
            enabled_owner_ids=frozenset({"00000000-0000-4000-8000-000000000099"}),
        )
        dependencies = build_phase6_recall_dependencies(
            services.phase5.session.get_bind(),
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            flags=flags,
            settings=Settings(),
        )
        assert dependencies.fts_index is None
        assert dependencies.vector_index is None
        assert dependencies.semantic_provider is None
        assert dependencies.repair_scheduler is None
        assert dependencies.metric_recorder is None
    finally:
        services.close()


def test_legacy_derived_side_effect_guard_follows_bound_owner_worker_policy(
    tmp_path, monkeypatch
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    add_memory(
        adapter,
        harness,
        key="phase6-legacy-derived-guard",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:legacy-derived-guard",
        text="synthetic legacy derived guard fixture",
    )
    services = phase6_services(harness)
    try:
        owner_id = services.phase5.repository.owner_id
        settings = Settings().model_copy(
            update={
                "memory_v2_outbox_worker_enabled": True,
                "memory_v2_enabled_owner_ids": owner_id,
            }
        )
        monkeypatch.setattr(memory_store_module, "get_settings", lambda: settings)
        store = MemoryStore(services.phase5.session)
        assert store._memory_v2_indexing_active
        store.set_memory_v2_indexing_active(False)
        assert store._memory_v2_indexing_active
        assert not store._ensure_memory_fts()

        outside = settings.model_copy(
            update={"memory_v2_enabled_owner_ids": ("00000000-0000-4000-8000-000000000099")}
        )
        monkeypatch.setattr(memory_store_module, "get_settings", lambda: outside)
        assert not MemoryStore(services.phase5.session)._memory_v2_indexing_active
    finally:
        services.close()


def test_provider_outage_keeps_canonical_and_fts_correct_with_pending_vector(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    imported = ImportMemoryV2Adapter(harness.coordinator).accept(
        harness.context,
        {
            "memory_type": "knowledge",
            "domain_key": "learning",
            "slot_key": ("knowledge:learning:item:22222222-2222-4222-8222-222222222222"),
            "cardinality": "additive",
            "canonical_value": "learn retrieval fixture imported",
            "display_text": "learn retrieval fixture imported",
        },
        batch_id="phase6-degraded-import",
        item_hash="synthetic-import-item",
    )
    assert imported.mutation is not None
    ids = [
        imported.mutation.active_memory_ids[-1],
        *[
            add_memory(
                adapter,
                harness,
                key=f"phase6-outage-{index}",
                memory_type=MemoryType.KNOWLEDGE,
                domain="learning",
                slot=f"knowledge:learning:outage_{index}",
                text=f"learn retrieval fixture {index}",
            )
            for index in range(2)
        ],
    ]
    services = phase6_services(harness)
    services.provider_source.fail = True
    try:
        while batch := services.processor.lease_batch(worker_id="outage-worker"):
            if not batch.leases:
                break
            services.processor.process_batch(batch)
        for memory_id in ids:
            assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).status == "active"
            assert services.fts.get_metadata(services.phase5.repository.owner_id, str(memory_id))
        maintenance = MemoryV2IndexMaintenance(
            services.phase5.session.get_bind(),
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            fts_index=services.fts,
            vector_index=services.vector,
            repair_scheduler=services.processor.schedule_repair,
            provider_health=services.provider.health,
            metric_reader=services.metrics.snapshot,
        )
        coverage = maintenance.coverage(now=datetime.now(UTC))
        assert coverage.fts_current_count == 3
        assert coverage.vector_current_count == 0
        assert coverage.failed_count == 3
        assert not coverage.rollout_ready
    finally:
        services.close()


def test_canonical_command_returns_without_invoking_blocking_provider(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    add_memory(
        adapter,
        harness,
        key="phase6-blocking-schema-initialization",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:blocking-init",
        text="synthetic schema initialization fixture",
    )
    provider_started = Event()
    release_provider = Event()
    services = phase6_services(harness)
    blocking = ValidatedMemoryV2EmbeddingProvider(
        BlockingEmbeddingSource(provider_started, release_provider), dimension=3
    )
    processor = MemoryV2OutboxProcessor(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=services.fts,
        vector_index=services.vector,
        embedding_provider=blocking,
    )
    try:
        result = adapter.create(
            harness.context,
            StructuredMemoryInput(
                memory_type=MemoryType.KNOWLEDGE,
                domain_key="learning",
                slot_key=("knowledge:learning:item:11111111-1111-4111-8111-111111111111"),
                cardinality=Cardinality.ADDITIVE,
                canonical_value="canonical write stays foreground-only",
                display_text="canonical write stays foreground-only",
            ),
            idempotency_key="phase6:blocking-provider-proof",
        )
        assert result.mutation is not None and result.mutation.active_memory_ids
        assert not provider_started.is_set()

        batch = processor.lease_batch(worker_id="blocking-worker")
        thread = Thread(target=processor.process_batch, args=(batch,))
        thread.start()
        assert provider_started.wait(timeout=2)
        assert thread.is_alive()
        memory_id = result.mutation.active_memory_ids[-1]
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).status == "active"
        release_provider.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        release_provider.set()
        services.close()
