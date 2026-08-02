from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from app.services.embeddings import ValidatedMemoryV2EmbeddingProvider
from app.services.memory_v2.indexes import SqliteMemoryV2FtsIndex, SqliteMemoryV2VectorIndex
from app.services.memory_v2.metrics import MemoryV2DerivedMetrics
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.phase6_contracts import RetryPolicy
from tests.memory_v2.phase5_helpers import Phase5Services, phase5_harness, phase5_services


class DeterministicEmbeddingProvider:
    provider_name = "deterministic"
    model_name = "phase6-fixture"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.healthy = True

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("fixture provider unavailable")
        digest = hashlib.sha256(text.encode()).digest()
        return [digest[0] / 255, digest[1] / 255, digest[2] / 255]

    def health(self) -> bool:
        return self.healthy


@dataclass
class Phase6Services:
    phase5: Phase5Services
    provider_source: DeterministicEmbeddingProvider
    provider: ValidatedMemoryV2EmbeddingProvider
    fts: SqliteMemoryV2FtsIndex
    vector: SqliteMemoryV2VectorIndex
    processor: MemoryV2OutboxProcessor
    metrics: MemoryV2DerivedMetrics

    def close(self) -> None:
        self.phase5.close()


def phase6_harness(tmp_path, **kwargs):
    harness, adapter = phase5_harness(tmp_path, **kwargs)
    harness.coordinator.flags = replace(
        harness.coordinator.flags,
        outbox_worker_enabled=True,
        fts_index_enabled=True,
        vector_index_enabled=True,
        semantic_recall_enabled=True,
        reconciliation_enabled=True,
    )
    return harness, adapter


def phase6_services(harness, *, retry_policy: RetryPolicy | None = None) -> Phase6Services:
    phase5 = phase5_services(harness)
    source = DeterministicEmbeddingProvider()
    provider = ValidatedMemoryV2EmbeddingProvider(source, dimension=3)
    engine = phase5.session.get_bind()
    fts = SqliteMemoryV2FtsIndex(engine)
    vector = SqliteMemoryV2VectorIndex(engine)
    processor = MemoryV2OutboxProcessor(
        engine,
        owner_id=phase5.repository.owner_id,
        database_identity=phase5.repository.database_identity,
        fts_index=fts,
        vector_index=vector,
        embedding_provider=provider,
        retry_policy=retry_policy,
    )
    metrics = MemoryV2DerivedMetrics(
        engine,
        owner_id=phase5.repository.owner_id,
        database_identity=phase5.repository.database_identity,
    )
    return Phase6Services(phase5, source, provider, fts, vector, processor, metrics)
