from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

import app.services.embeddings as embeddings_module
from app.models.memory import (
    MemoryDerivedState,
    MemoryOutbox,
    MemoryRecord,
    MemoryVectorPoint,
)
from app.services.embeddings import (
    EmbeddingValidationError,
    OllamaEmbeddingProvider,
    ValidatedMemoryEmbeddingProvider,
)
from app.services.memory.index_contracts import (
    DerivedFailureCode,
    DerivedTarget,
    DerivedTargetState,
    IndexRepairRequest,
    ProviderHealth,
)
from app.services.memory.indexes import DerivedDocumentBuilder
from app.services.memory.taxonomy import MemoryType
from tests.memory.index_helpers import index_harness, index_services
from tests.memory.recall_helpers import add_memory


class InvalidEmbeddingSource:
    provider_name = "invalid-fixture"
    model_name = "invalid-fixture-model"

    def __init__(self, vector) -> None:
        self.vector = vector

    def embed(self, _text):
        return self.vector


class MissingEmbeddingIdentitySource:
    provider_name = ""
    model_name = ""

    def embed(self, _text):
        return [0.1, 0.2, 0.3]


class StructuredHealthEmbeddingSource:
    provider_name = "structured-health-fixture"
    model_name = "structured-health-model"

    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    def embed(self, _text):
        return [1.0, 0.0, 0.0]

    def health(self):
        return ProviderHealth(
            provider=self.provider_name,
            model=self.model_name,
            provider_version="1",
            healthy=self.healthy,
            failure_code=None if self.healthy else "embedding_unavailable",
        )


def test_derived_document_hash_is_deterministic_and_contains_only_approved_fields(
    tmp_path,
) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="document-hash",
        memory_type=MemoryType.KNOWLEDGE,
        domain="software_development",
        slot="knowledge:software_development:testing",
        text="use deterministic integration tests",
    )
    services = index_services(harness)
    try:
        record = services.recall.session.get(MemoryRecord, str(memory_id))
        builder = DerivedDocumentBuilder()
        first = builder.build(record, now=datetime.now(UTC))
        second = builder.build(record, now=datetime.now(UTC))
        assert first == second
        payload = first.model_dump()
        assert set(payload) == {
            "schema_version",
            "memory_id",
            "owner_id",
            "content_hash",
            "canonical_content_hash",
            "canonical_revision",
            "memory_type",
            "domain_key",
            "slot_key",
            "display_text",
        }
        assert "source" not in first.model_dump_json()
        assert "operation" not in first.model_dump_json()
    finally:
        services.close()


def test_derived_and_embedding_document_hashes_match_frozen_fixture() -> None:
    record = MemoryRecord(
        id="11111111-1111-4111-8111-111111111111",
        owner_id="00000000-0000-4000-8000-000000000001",
        status="active",
        expires_at=None,
        sensitivity="normal",
        display_text="use deterministic integration tests",
        revision=7,
        memory_type="knowledge",
        domain_key="software_development",
        slot_key=("knowledge:software_development:item:11111111-1111-4111-8111-111111111111"),
        canonical_fingerprint="fixture-canonical-hash",
    )
    builder = DerivedDocumentBuilder()
    document = builder.build(record, now=datetime(2026, 1, 1, tzinfo=UTC))
    embedding = builder.build_embedding(document)
    assert document.content_hash == (
        "1e57f8dd364cc29075229962b092503e0a5140598a3d6fabfa308eb60142ecdc"
    )
    assert embedding.content_hash == (
        "2ca4cc1eb6aeee8a74056ed2c866a2c6f1ee45f665a42383477d7ea01a58061c"
    )
    assert embedding.text == record.display_text


def test_repeated_vector_upsert_has_one_point_and_old_delete_cannot_remove_new_hash(
    tmp_path,
) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="vector-idempotency",
        memory_type=MemoryType.GOAL,
        domain="video_creation",
        slot="goal:video_creation:current_primary_goal",
        text="create short cinematic reels",
    )
    services = index_services(harness)
    try:
        record = services.recall.session.get(MemoryRecord, str(memory_id))
        old = services.processor.document_builder.build(record, now=datetime.now(UTC))
        vector = services.provider.embed(old.display_text)
        services.vector.upsert(old, vector, services.provider)
        services.vector.upsert(old, vector, services.provider)
        count = services.recall.session.scalar(select(func.count(MemoryVectorPoint.id)))
        assert count == 1
        services.recall.session.rollback()

        services.recall.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(memory_id))
            .values(display_text="create current short reels", revision=2)
        )
        services.recall.session.commit()
        services.recall.session.refresh(record)
        new = services.processor.document_builder.build(record, now=datetime.now(UTC))
        services.vector.upsert(new, services.provider.embed(new.display_text), services.provider)
        assert not services.vector.delete(
            services.recall.repository.owner_id,
            str(memory_id),
            expected_hash=old.content_hash,
        )
        metadata = services.vector.get_metadata(services.recall.repository.owner_id, str(memory_id))
        assert metadata["content_hash"] == new.content_hash
        assert metadata["canonical_revision"] == 2
    finally:
        services.close()


def test_delayed_worker_delete_cannot_remove_or_mark_newer_vector_deleted(tmp_path) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="worker-delete-race",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:worker-delete-race",
        text="old synthetic vector fixture",
    )
    services = index_services(harness)
    try:
        services.processor.process_batch(
            services.processor.lease_batch(worker_id="initial-index-worker")
        )
        owner_id = services.recall.repository.owner_id
        old = services.vector.get_metadata(owner_id, str(memory_id))

        services.recall.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id == str(memory_id))
            .values(
                display_text="new synthetic vector fixture",
                revision=2,
                canonical_fingerprint="new-canonical-fingerprint",
            )
        )
        services.recall.session.commit()
        services.processor.schedule_repair(
            IndexRepairRequest(
                owner_id=owner_id,
                memory_id=memory_id,
                action="upsert",
                target=DerivedTarget.VECTOR,
                reason="fixture_newer_upsert",
            )
        )
        services.processor.process_batch(
            services.processor.lease_batch(worker_id="new-index-worker")
        )
        new = services.vector.get_metadata(owner_id, str(memory_id))
        assert new["content_hash"] != old["content_hash"]

        services.processor.schedule_repair(
            IndexRepairRequest(
                owner_id=owner_id,
                memory_id=memory_id,
                action="delete",
                target=DerivedTarget.VECTOR,
                reason="fixture_delayed_stale_delete",
                expected_hash=old["content_hash"],
            )
        )
        batch = services.processor.lease_batch(worker_id="delayed-delete-worker")
        result = services.processor.process_batch(batch)[0]

        persisted = services.vector.get_metadata(owner_id, str(memory_id))
        assert persisted["content_hash"] == new["content_hash"]
        state = services.recall.session.scalar(
            select(MemoryDerivedState).where(
                MemoryDerivedState.owner_id == owner_id,
                MemoryDerivedState.memory_id == str(memory_id),
                MemoryDerivedState.target == DerivedTarget.VECTOR.value,
            )
        )
        assert state.state == DerivedTargetState.CURRENT.value
        assert state.content_hash == new["content_hash"]
        assert result.diagnostics[0].to_state is DerivedTargetState.NOT_APPLICABLE
        assert result.diagnostics[0].repair_reason == (
            DerivedFailureCode.CANONICAL_HASH_ADVANCED.value
        )
        event = services.recall.session.scalar(
            select(MemoryOutbox).where(
                MemoryOutbox.id == str(result.event_id),
                MemoryOutbox.state == "done",
            )
        )
        assert event is not None
    finally:
        services.close()


def test_fts5_namespace_returns_only_bounded_owner_hash_candidates(tmp_path) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="fts5-owner-candidate",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:fts5",
        text="practice deterministic retrieval drills",
    )
    services = index_services(harness)
    try:
        services.processor.process_batch(services.processor.lease_batch(worker_id="fts5-worker"))
        assert services.fts.health().healthy
        hits = services.fts.search(
            services.recall.repository.owner_id,
            "deterministic retrieval",
            1,
        )
        assert len(hits) == 1
        assert hits[0]["memory_id"] == str(memory_id)
        assert hits[0]["owner_id"] == services.recall.repository.owner_id
        assert set(hits[0]) == {"owner_id", "memory_id", "content_hash", "score"}
    finally:
        services.close()


@pytest.mark.parametrize(
    ("vector", "code"),
    [
        ([1.0, 2.0], "embedding_dimension_mismatch"),
        ([1.0, float("nan"), 3.0], "embedding_invalid_response"),
        ([1.0, float("inf"), 3.0], "embedding_invalid_response"),
    ],
)
def test_embedding_boundary_rejects_wrong_dimension_and_non_finite_values(vector, code) -> None:
    provider = ValidatedMemoryEmbeddingProvider(InvalidEmbeddingSource(vector), dimension=3)
    with pytest.raises(EmbeddingValidationError, match=code):
        provider.embed("synthetic approved fixture")
    assert provider.consecutive_failures == 1


def test_embedding_boundary_rejects_missing_provider_identity() -> None:
    with pytest.raises(ValueError, match="embedding_provider_identity_invalid"):
        ValidatedMemoryEmbeddingProvider(MissingEmbeddingIdentitySource(), dimension=3)


@pytest.mark.parametrize("healthy", [False, True])
def test_embedding_boundary_respects_structured_provider_health(healthy) -> None:
    provider = ValidatedMemoryEmbeddingProvider(
        StructuredHealthEmbeddingSource(healthy),
        dimension=3,
    )
    assert provider.health() is healthy


def test_vector_adapter_rejects_vector_that_disagrees_with_provider_dimension(tmp_path) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="vector-adapter-dimension",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:vector-adapter-dimension",
        text="synthetic vector adapter dimension fixture",
    )
    services = index_services(harness)
    try:
        record = services.recall.session.get(MemoryRecord, str(memory_id))
        document = DerivedDocumentBuilder().build(record, now=datetime.now(UTC))
        with pytest.raises(ValueError, match="embedding_dimension_mismatch"):
            services.vector.upsert(document, [0.1, 0.2], services.provider)
        assert (
            services.vector.get_metadata(
                services.recall.repository.owner_id,
                str(memory_id),
            )
            is None
        )
    finally:
        services.close()


def test_sqlite_vector_search_returns_bounded_top_k_with_stable_ties(tmp_path) -> None:
    harness, adapter = index_harness(tmp_path)
    memory_ids = [
        add_memory(
            adapter,
            harness,
            key=f"phase6-vector-top-k-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:vector_top_k_{index}",
            text=f"synthetic vector top k fixture {index}",
        )
        for index in range(4)
    ]
    services = index_services(harness)
    try:
        documents = [
            services.processor.document_builder.build(
                services.recall.session.get(MemoryRecord, str(memory_id)),
                now=datetime.now(UTC),
            )
            for memory_id in memory_ids
        ]
        for index, document in enumerate(documents):
            services.vector.upsert(
                document,
                [1.0, 0.0, 0.0] if index == 0 else [0.0, 1.0, 0.0],
                services.provider,
            )
        first = services.vector.search(
            [1.0, 0.0, 0.0],
            services.recall.repository.owner_id,
            limit=2,
        )
        second = services.vector.search(
            [1.0, 0.0, 0.0],
            services.recall.repository.owner_id,
            limit=2,
        )
        expected_tie = min(memory_ids[1:], key=str)
        assert [item.memory_id for item in first] == [memory_ids[0], expected_tie]
        assert [item.memory_id for item in second] == [memory_ids[0], expected_tie]
        assert len(first) == 2
        assert [item.score for item in first] == [1.0, 0.0]
    finally:
        services.close()


def test_ollama_health_requires_reachable_exact_configured_model(monkeypatch) -> None:
    class Response:
        ok = True

        @staticmethod
        def json():
            return {"models": [{"name": "nomic-embed-text:latest"}]}

    monkeypatch.setattr(embeddings_module.requests, "get", lambda *_args, **_kwargs: Response())
    provider = OllamaEmbeddingProvider(
        model_name="nomic-embed-text:latest",
        base_url="http://127.0.0.1:11434",
        timeout=5,
    )
    assert provider.health()
    provider.model_name = "missing-fixture-model:latest"
    assert not provider.health()
