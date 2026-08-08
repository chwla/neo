"""Optional semantic and lexical recall wiring with no eager provider calls."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.services.embeddings import (
    OllamaEmbeddingProvider,
    ValidatedMemoryEmbeddingProvider,
)
from app.services.memory.index_contracts import RetryPolicy
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.metrics import MemoryDerivedMetrics
from app.services.memory.outbox import MemoryOutboxProcessor
from app.services.memory.settings import MemorySettings


@dataclass(frozen=True)
class MemoryRecallDependencies:
    fts_index: object | None = None
    vector_index: object | None = None
    semantic_provider: object | None = None
    repair_scheduler: object | None = None
    metric_recorder: object | None = None


def build_memory_recall_dependencies(
    engine: Engine,
    *,
    owner_id: str,
    database_identity: str,
    flags: MemorySettings,
    settings: Settings,
) -> MemoryRecallDependencies:
    """Construct enabled owner-scoped recall dependencies."""
    if not flags.owner_is_enabled(owner_id):
        return MemoryRecallDependencies()
    fts = SqliteMemoryFtsIndex(engine) if flags.fts_index_enabled else None
    if not flags.semantic_recall_enabled:
        return MemoryRecallDependencies(fts_index=fts)
    if settings.memory_embedding_provider != "ollama":
        return MemoryRecallDependencies(fts_index=fts)
    raw_provider = OllamaEmbeddingProvider(
        model_name=settings.memory_embedding_model,
        base_url=settings.ollama_url,
        timeout=settings.memory_query_embedding_timeout_seconds,
    )
    provider = ValidatedMemoryEmbeddingProvider(
        raw_provider,
        dimension=settings.memory_embedding_dimension,
        provider_version=settings.memory_embedding_version,
        cooldown_seconds=settings.memory_provider_cooldown_seconds,
    )
    vector = SqliteMemoryVectorIndex(engine)
    metrics = MemoryDerivedMetrics(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
    )
    processor = MemoryOutboxProcessor(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
        fts_index=fts,
        vector_index=vector,
        embedding_provider=provider,
        retry_policy=RetryPolicy(
            maximum_attempts=settings.memory_retry_max_attempts,
            dead_letter_threshold=settings.memory_dead_letter_threshold,
            base_delay_seconds=settings.memory_retry_base_seconds,
            maximum_delay_seconds=settings.memory_retry_max_seconds,
            jitter_seconds=settings.memory_retry_jitter_seconds,
            lease_seconds=settings.memory_worker_lease_seconds,
            batch_size=settings.memory_worker_batch_size,
        ),
    )
    return MemoryRecallDependencies(
        fts_index=fts,
        vector_index=vector,
        semantic_provider=provider,
        repair_scheduler=processor.schedule_repair,
        metric_recorder=metrics.record,
    )
