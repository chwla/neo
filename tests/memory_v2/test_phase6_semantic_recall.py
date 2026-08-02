from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update

from app.models.memory_v2 import MemoryRecordV2
from app.services.direct_answer import DirectMemoryAnswerService
from app.services.embeddings import ValidatedMemoryV2EmbeddingProvider
from app.services.memory_v2.contracts import ReplacementAuthority, Sensitivity, TargetRevision
from app.services.memory_v2.extraction_contracts import CurrentTurnOverride
from app.services.memory_v2.indexes import DerivedDocumentBuilder
from app.services.memory_v2.phase6_contracts import VectorCandidate
from app.services.memory_v2.prompt import RecallPromptOrchestrator, repository_usage_recorder
from app.services.memory_v2.queries import RecallMode, RecallQuery
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.taxonomy import MemoryType
from app.services.memory_v2.versions import VECTOR_METADATA_VERSION
from app.services.research.memory_scope import retrieve_scoped_memory_result
from tests.memory_v2.phase3_helpers import OWNER_B, video_goal
from tests.memory_v2.phase5_helpers import add_memory, query_context
from tests.memory_v2.phase6_helpers import phase6_harness, phase6_services


class FixtureVectorIndex:
    def __init__(self, hits=(), *, fail=False) -> None:
        self.hits = list(hits)
        self.fail = fail
        self.calls = 0

    def search(self, _vector, _owner_id, _limit):
        self.calls += 1
        if self.fail:
            raise RuntimeError("vector unavailable")
        return list(self.hits)


class FailingSearchFts:
    def search(self, *_args):
        raise RuntimeError("fts_unavailable")


def _hit(document, provider, *, score=1, owner_id=None, content_hash=None, revision=None):
    embedding_document = DerivedDocumentBuilder.build_embedding(document)
    return VectorCandidate(
        owner_id=UUID(owner_id) if owner_id else document.owner_id,
        memory_id=document.memory_id,
        content_hash=content_hash or document.content_hash,
        canonical_revision=revision or document.canonical_revision,
        score=score,
        provider=provider.provider_name,
        model=provider.model_name,
        provider_version=provider.provider_version,
        dimension=provider.dimension,
        metadata_version=VECTOR_METADATA_VERSION,
        derived_schema_version=document.schema_version,
        embedding_document_version=embedding_document.version,
        embedding_content_hash=embedding_document.content_hash,
    )


def _recall(services, hits, repairs):
    vector = FixtureVectorIndex(hits)
    recall = CanonicalRecallService(
        services.phase5.repository,
        flags=services.phase5.harness.coordinator.flags,
        semantic_provider=services.provider,
        vector_index=vector,
        repair_scheduler=repairs.append,
        metric_recorder=services.metrics.record,
    )
    return recall, vector


def test_stale_superseded_predecessor_is_dropped_for_active_successor(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    old_id = add_memory(
        adapter,
        harness,
        key="semantic-old",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create long-form cinematic videos",
    )
    services = phase6_services(harness)
    try:
        old = services.phase5.session.get(MemoryRecordV2, str(old_id))
        old_document = services.processor.document_builder.build(old, now=datetime.now(UTC))
        replacement = adapter.replace(
            harness.context,
            video_goal("create short Instagram reels"),
            (TargetRevision(memory_id=old_id, expected_revision=1),),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            idempotency_key="phase6:semantic-real-replacement",
        )
        assert replacement.mutation is not None
        new_id = replacement.mutation.active_memory_ids[-1]
        services.phase5.session.expire_all()
        repairs = []
        recall, _vector = _recall(services, [_hit(old_document, services.provider)], repairs)
        result = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="create short Instagram reels",
            )
        )
        assert result.canonical_ids == (new_id,)
        assert old_id not in result.canonical_ids
        assert result.diagnostic.semantic_inactive_drop_count == 1
        assert repairs[0].action == "delete"
        assert services.phase5.session.get(MemoryRecordV2, str(old_id)).usage_count == 0
    finally:
        services.close()


def test_ghost_and_wrong_owner_hits_do_not_consume_or_suppress_local_result(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path / "owner-a")
    local_id = add_memory(
        adapter,
        harness,
        key="semantic-local",
        memory_type=MemoryType.KNOWLEDGE,
        domain="video_creation",
        slot="knowledge:video_creation:local",
        text="use match cuts for video reels",
    )
    services = phase6_services(harness)
    other_harness, other_adapter = phase6_harness(
        tmp_path / "owner-b",
        owner_id=OWNER_B,
        profile_id="owner-b",
    )
    other_id = add_memory(
        other_adapter,
        other_harness,
        key="semantic-other-owner",
        memory_type=MemoryType.KNOWLEDGE,
        domain="video_creation",
        slot="knowledge:video_creation:other-owner",
        text="use match cuts for video reels",
    )
    other_services = phase6_services(other_harness)
    try:
        local = services.phase5.session.get(MemoryRecordV2, str(local_id))
        document = services.processor.document_builder.build(local, now=datetime.now(UTC))
        ghost = document.model_copy(update={"memory_id": uuid4()})
        other_record = other_services.phase5.session.get(MemoryRecordV2, str(other_id))
        other_document = other_services.processor.document_builder.build(
            other_record, now=datetime.now(UTC)
        )
        other_services.processor.process_batch(
            other_services.processor.lease_batch(worker_id="owner-b-worker")
        )
        wrong = _hit(other_document, services.provider)
        repairs = []

        def schedule(request):
            repairs.append(request)
            services.processor.schedule_repair(request)

        malformed_secret = "malformed-private-provider-payload"
        vector = FixtureVectorIndex(
            [
                {
                    "owner_id": services.phase5.repository.owner_id,
                    "memory_id": "not-a-uuid",
                    "content_hash": malformed_secret,
                },
                _hit(ghost, services.provider),
                wrong,
                _hit(document, services.provider),
            ]
        )
        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            semantic_provider=services.provider,
            vector_index=vector,
            repair_scheduler=schedule,
            metric_recorder=services.metrics.record,
        )
        result = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                    maximum_records=1,
                ),
                text="video reels match cuts",
            )
        )
        assert result.canonical_ids == (local_id,)
        assert result.diagnostic.semantic_ghost_drop_count == 1
        assert result.diagnostic.semantic_wrong_owner_drop_count == 1
        assert result.diagnostic.semantic_stale_drop_count == 1
        assert malformed_secret not in result.diagnostic.model_dump_json()
        assert len(repairs) == 2
        assert all(str(item.owner_id) == services.phase5.repository.owner_id for item in repairs)
        while batch := services.processor.lease_batch(worker_id="owner-a-repair-worker"):
            if not batch.leases:
                break
            services.processor.process_batch(batch)
        assert (
            other_services.vector.get_metadata(
                other_services.phase5.repository.owner_id, str(other_id)
            )
            is not None
        )
        other_services.phase5.session.expire_all()
        assert other_services.phase5.session.get(MemoryRecordV2, str(other_id)).usage_count == 0
        snapshot = services.metrics.snapshot()
        assert snapshot["semantic_wrong_owner_hit"] == 1
    finally:
        services.close()
        other_services.close()


def test_hash_mismatch_is_rejected_and_current_upsert_repair_is_scheduled(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-hash",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:hash",
        text="learn current retrieval methods",
    )
    services = phase6_services(harness)
    try:
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        document = services.processor.document_builder.build(record, now=datetime.now(UTC))
        repairs = []
        recall, _vector = _recall(
            services,
            [_hit(document, services.provider, content_hash="0" * 64)],
            repairs,
        )
        result = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"learning"})),
                text="quantum orchid",
            )
        )
        assert memory_id not in result.canonical_ids
        assert result.diagnostic.semantic_stale_drop_count == 1
        assert repairs[0].action == "upsert"
        assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).revision == 1
    finally:
        services.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"dimension": 99},
        {"metadata_version": "obsolete-vector-metadata"},
        {"derived_schema_version": "obsolete-derived-schema"},
        {"embedding_document_version": "obsolete-embedding-document"},
        {"embedding_content_hash": "f" * 64},
        {"embedding_identity_version": "obsolete-embedding-identity"},
    ],
)
def test_untrusted_vector_metadata_mismatch_is_rejected_and_repaired(tmp_path, changes) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key=f"semantic-metadata-{next(iter(changes))}",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:metadata",
        text="synthetic vector metadata validation fixture",
    )
    services = phase6_services(harness)
    try:
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        document = services.processor.document_builder.build(record, now=datetime.now(UTC))
        stale_hit = _hit(document, services.provider).model_copy(update=changes)
        repairs = []
        recall, _vector = _recall(services, [stale_hit], repairs)
        result = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"learning"})),
                text="unrelated lexical query",
            )
        )
        assert memory_id not in result.canonical_ids
        assert result.diagnostic.semantic_stale_drop_count == 1
        assert repairs[0].action == "upsert"
        assert repairs[0].target.value == "vector"
    finally:
        services.close()


def test_embedding_and_vector_outages_degrade_to_lexical_and_deterministic(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-outage",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short cinematic reels",
    )
    services = phase6_services(harness)
    try:
        recall, vector = _recall(services, [], [])
        services.provider_source.fail = True
        lexical = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="short cinematic reels",
            )
        )
        assert lexical.canonical_ids == (memory_id,)
        assert lexical.diagnostic.degraded_semantic_reason == "semantic_unavailable"
        prompt = RecallPromptOrchestrator(
            recall,
            usage_recorder=repository_usage_recorder(services.phase5.repository),
        ).build(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="short cinematic reels",
            ),
            purpose="phase6-vector-outage",
        )
        assert prompt.serialized is not None
        assert prompt.serialized.canonical_ids == (memory_id,)
        direct = DirectMemoryAnswerService(
            canonical_recall=recall,
            memory_v2_enabled=True,
        ).answer(
            object(),
            "What are my current goals?",
            query_context=query_context(
                services.phase5,
                domains=frozenset({"video_creation"}),
            ),
        )
        assert direct is not None and "create short cinematic reels" in direct
        research = retrieve_scoped_memory_result(
            "Research short cinematic reels for my goals",
            v2_enabled=True,
            orchestrator=RecallPromptOrchestrator(
                recall,
                usage_recorder=repository_usage_recorder(services.phase5.repository),
            ),
            query_context=query_context(
                services.phase5,
                domains=frozenset({"video_creation"}),
            ),
            usage_purpose="phase6-research-outage",
        )
        assert memory_id in {UUID(item) for item in research.canonical_ids}

        before = services.provider_source.calls
        deterministic = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, mode=RecallMode.DETERMINISTIC),
                canonical_id=memory_id,
            )
        )
        assert deterministic.canonical_ids == (memory_id,)
        assert services.provider_source.calls == before

        services.provider_source.fail = False
        vector.fail = True
        lexical_again = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                ),
                text="short cinematic reels",
            )
        )
        assert lexical_again.canonical_ids == (memory_id,)
        assert lexical_again.diagnostic.degraded_semantic_reason == "semantic_unavailable"
    finally:
        services.close()


def test_fts_failure_still_allows_validated_semantic_recall(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-fts-outage",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:semantic-fts-outage",
        text="synthetic semantic-only retrieval fixture",
    )
    services = phase6_services(harness)
    try:
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        document = services.processor.document_builder.build(record, now=datetime.now(UTC))
        vector = FixtureVectorIndex([_hit(document, services.provider)])
        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            fts_index=FailingSearchFts(),
            semantic_provider=services.provider,
            vector_index=vector,
        )
        result = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"learning"})),
                text="quantum orchid",
            )
        )
        assert result.canonical_ids == (memory_id,)
        assert result.diagnostic.degraded_lexical
        assert result.diagnostic.semantic_candidate_count == 1
        assert result.diagnostic.semantic_validated_count == 1
        assert result.diagnostic.degraded_semantic_reason is None
    finally:
        services.close()


def test_provider_cooldown_skips_repeated_query_embedding_attempt(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-provider-cooldown",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:semantic-provider-cooldown",
        text="synthetic provider cooldown fixture",
    )
    services = phase6_services(harness)
    try:
        services.provider_source.fail = True
        provider = ValidatedMemoryV2EmbeddingProvider(
            services.provider_source,
            dimension=3,
            cooldown_seconds=60,
            clock=lambda: 100.0,
        )
        vector = FixtureVectorIndex()
        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            semantic_provider=provider,
            vector_index=vector,
        )
        query = RecallQuery(
            context=query_context(services.phase5, domains=frozenset({"learning"})),
            text="provider cooldown fixture",
        )
        first = recall.recall(query)
        assert first.canonical_ids == (memory_id,)
        assert first.diagnostic.degraded_semantic_reason == "semantic_unavailable"
        calls = services.provider_source.calls

        second = recall.recall(query)
        assert second.canonical_ids == (memory_id,)
        assert second.diagnostic.degraded_semantic_reason == "embedding_unhealthy"
        assert services.provider_source.calls == calls
        assert vector.calls == 0
    finally:
        services.close()


def test_current_turn_suppression_removes_semantic_candidate(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-suppression",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create long-form cinematic videos",
    )
    services = phase6_services(harness)
    try:
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        document = services.processor.document_builder.build(record, now=datetime.now(UTC))
        recall, _vector = _recall(services, [_hit(document, services.provider)], [])
        override = CurrentTurnOverride(
            owner_id=services.phase5.repository.owner_id,
            source_message_id="phase6-current-turn",
            suppressed_memory_ids=(memory_id,),
            suppressed_slot_keys=(record.slot_key,),
            contradicted_memory_ids=(memory_id,),
            contradicted_slot_keys=(record.slot_key,),
            contradiction_deterministic=True,
        )
        result = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                    override=override,
                ),
                text="cinematic videos",
            )
        )
        assert not result.items
        assert result.diagnostic.suppressed_ids == (memory_id,)
    finally:
        services.close()


def test_expired_semantic_hit_is_rejected_without_usage_and_queues_delete(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="semantic-expired",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:expired",
        text="expired semantic retrieval fixture",
    )
    services = phase6_services(harness)
    try:
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        document = services.processor.document_builder.build(record, now=datetime.now(UTC))
        services.phase5.session.execute(
            update(MemoryRecordV2)
            .where(MemoryRecordV2.id == str(memory_id))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.phase5.session.commit()
        repairs = []
        recall, _vector = _recall(services, [_hit(document, services.provider)], repairs)
        result = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"learning"})),
                text="expired semantic retrieval fixture",
            )
        )
        assert memory_id not in result.canonical_ids
        assert result.diagnostic.semantic_inactive_drop_count == 1
        assert repairs[0].action == "delete"
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, str(memory_id)).usage_count == 0
    finally:
        services.close()


def test_sensitive_semantic_hit_is_rejected_and_persisted_vector_is_deleted(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    sensitive_id = add_memory(
        adapter,
        harness,
        key="semantic-sensitive",
        memory_type=MemoryType.KNOWLEDGE,
        domain="health_fitness",
        slot="knowledge:health_fitness:semantic_sensitive",
        text="synthetic sensitive semantic fixture",
        sensitivity=Sensitivity.SENSITIVE,
    )
    donor_id = add_memory(
        adapter,
        harness,
        key="semantic-sensitive-donor",
        memory_type=MemoryType.KNOWLEDGE,
        domain="health_fitness",
        slot="knowledge:health_fitness:semantic_donor",
        text="synthetic ordinary semantic fixture",
    )
    services = phase6_services(harness)
    try:
        donor = services.phase5.session.get(MemoryRecordV2, str(donor_id))
        donor_document = services.processor.document_builder.build(
            donor,
            now=datetime.now(UTC),
        )
        untrusted_document = donor_document.model_copy(update={"memory_id": sensitive_id})
        services.vector.upsert(
            untrusted_document,
            services.provider.embed(untrusted_document.display_text),
            services.provider,
        )
        assert services.vector.get_metadata(
            services.phase5.repository.owner_id,
            str(sensitive_id),
        )
        repairs = []

        def schedule(request):
            repairs.append(request)
            services.processor.schedule_repair(request)

        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            semantic_provider=services.provider,
            vector_index=FixtureVectorIndex([_hit(untrusted_document, services.provider)]),
            repair_scheduler=schedule,
            metric_recorder=services.metrics.record,
        )
        result = recall.recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"health_fitness"}),
                ),
                text="synthetic sensitive semantic fixture",
            )
        )
        assert sensitive_id not in result.canonical_ids
        assert result.diagnostic.semantic_inactive_drop_count == 1
        assert result.diagnostic.semantic_repair_count == 1
        assert len(repairs) == 1
        assert repairs[0].action == "delete"
        assert repairs[0].reason == "semantic_policy_ineligible"

        while batch := services.processor.lease_batch(worker_id="sensitive-cleanup-worker"):
            if not batch.leases:
                break
            services.processor.process_batch(batch)
        assert (
            services.vector.get_metadata(
                services.phase5.repository.owner_id,
                str(sensitive_id),
            )
            is None
        )
        services.phase5.session.expire_all()
        sensitive = services.phase5.session.get(MemoryRecordV2, str(sensitive_id))
        assert sensitive.status == "active"
        assert sensitive.usage_count == 0
    finally:
        services.close()


def test_hybrid_merge_respects_record_text_budgets_and_usage_matches_serialized_ids(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_ids = [
        add_memory(
            adapter,
            harness,
            key=f"semantic-budget-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:budget_{index}",
            text=f"practice semantic retrieval fixture number {index}",
        )
        for index in range(7)
    ]
    services = phase6_services(harness)
    try:
        documents = [
            services.processor.document_builder.build(
                services.phase5.session.get(MemoryRecordV2, str(memory_id)),
                now=datetime.now(UTC),
            )
            for memory_id in memory_ids
        ]
        recall, _vector = _recall(
            services,
            [_hit(document, services.provider) for document in documents],
            [],
        )
        prompt = RecallPromptOrchestrator(
            recall,
            usage_recorder=repository_usage_recorder(services.phase5.repository),
        )
        context = query_context(
            services.phase5,
            domains=frozenset({"learning"}),
            maximum_records=5,
            maximum_characters=900,
        )
        selection = prompt.build(
            RecallQuery(context=context, text="practice semantic retrieval fixture"),
            purpose="phase6-budget",
        )
        assert selection.serialized is not None
        assert len(selection.serialized.canonical_ids) <= 5
        assert selection.serialized.character_count <= 900
        assert selection.recall.diagnostic.usage_event_ids == selection.serialized.canonical_ids
        assert (
            tuple(item.canonical_id for item in selection.recall.diagnostic.score_components)
            == selection.recall.canonical_ids
        )
        diagnostic_json = selection.recall.diagnostic.model_dump_json()
        assert "practice semantic retrieval fixture" not in diagnostic_json
        selected = set(selection.serialized.canonical_ids)
        services.phase5.session.expire_all()
        for memory_id in memory_ids:
            usage = services.phase5.session.get(MemoryRecordV2, str(memory_id)).usage_count
            assert usage == int(memory_id in selected)
    finally:
        services.close()


def test_semantic_domain_gate_beats_high_score_pin_and_duplicate_ids_merge(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    local_id = add_memory(
        adapter,
        harness,
        key="semantic-domain-local",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:semantic_domain_local",
        text="local scoped semantic candidate",
    )
    unrelated_id = add_memory(
        adapter,
        harness,
        key="semantic-domain-unrelated",
        memory_type=MemoryType.KNOWLEDGE,
        domain="software_development",
        slot="knowledge:software_development:semantic_domain_unrelated",
        text="unrelated pinned semantic candidate",
    )
    services = phase6_services(harness)
    try:
        services.phase5.session.execute(
            update(MemoryRecordV2)
            .where(MemoryRecordV2.id == str(unrelated_id))
            .values(pinned=True, importance=10)
        )
        services.phase5.session.commit()
        local = services.phase5.session.get(MemoryRecordV2, str(local_id))
        unrelated = services.phase5.session.get(MemoryRecordV2, str(unrelated_id))
        local_document = services.processor.document_builder.build(
            local,
            now=datetime.now(UTC),
        )
        unrelated_document = services.processor.document_builder.build(
            unrelated,
            now=datetime.now(UTC),
        )
        recall, _vector = _recall(
            services,
            [
                _hit(unrelated_document, services.provider, score=1),
                _hit(local_document, services.provider, score=0.2),
                _hit(local_document, services.provider, score=0.8),
            ],
            [],
        )
        query = RecallQuery(
            context=query_context(
                services.phase5,
                domains=frozenset({"learning"}),
            ),
            text="synthetic retrieval phrase",
        )
        first = recall.recall(query)
        second = recall.recall(query)
        assert first.canonical_ids == second.canonical_ids == (local_id,)
        assert unrelated_id not in first.canonical_ids
        assert first.diagnostic.semantic_candidate_count == 3
        assert first.diagnostic.semantic_validated_count == 2
        assert len(first.diagnostic.score_components) == 1
        assert first.diagnostic.score_components[0].score.semantic == pytest.approx(0.9)
    finally:
        services.close()
