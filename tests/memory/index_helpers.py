from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.services.embeddings import ValidatedMemoryEmbeddingProvider
from app.services.memory.index_contracts import RetryPolicy
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.metrics import MemoryDerivedMetrics
from app.services.memory.outbox import MemoryOutboxProcessor
from tests.memory.recall_helpers import RecallServices, recall_harness, recall_services


class DeterministicEmbeddingProvider:
    provider_name = "deterministic"
    model_name = "index-fixture"

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
class IndexServices:
    recall: RecallServices
    provider_source: DeterministicEmbeddingProvider
    provider: ValidatedMemoryEmbeddingProvider
    fts: SqliteMemoryFtsIndex
    vector: SqliteMemoryVectorIndex
    processor: MemoryOutboxProcessor
    metrics: MemoryDerivedMetrics

    def close(self) -> None:
        self.recall.close()


def index_harness(tmp_path, **kwargs):
    harness, adapter = recall_harness(tmp_path, **kwargs)
    return harness, adapter


def index_services(harness, *, retry_policy: RetryPolicy | None = None) -> IndexServices:
    recall = recall_services(harness)
    source = DeterministicEmbeddingProvider()
    provider = ValidatedMemoryEmbeddingProvider(source, dimension=3)
    engine = recall.session.get_bind()
    fts = SqliteMemoryFtsIndex(engine)
    vector = SqliteMemoryVectorIndex(engine)
    processor = MemoryOutboxProcessor(
        engine,
        owner_id=recall.repository.owner_id,
        database_identity=recall.repository.database_identity,
        fts_index=fts,
        vector_index=vector,
        embedding_provider=provider,
        retry_policy=retry_policy,
    )
    metrics = MemoryDerivedMetrics(
        engine,
        owner_id=recall.repository.owner_id,
        database_identity=recall.repository.database_identity,
    )
    return IndexServices(recall, source, provider, fts, vector, processor, metrics)
