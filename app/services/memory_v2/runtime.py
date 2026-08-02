"""Flag-gated Phase 6 recall wiring with no eager provider calls."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.services.embeddings import (
    OllamaEmbeddingProvider,
    ValidatedMemoryV2EmbeddingProvider,
)
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.indexes import SqliteMemoryV2FtsIndex, SqliteMemoryV2VectorIndex
from app.services.memory_v2.metrics import MemoryV2DerivedMetrics
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.phase6_contracts import RetryPolicy


@dataclass(frozen=True)
class Phase6RecallDependencies:
    fts_index: object | None = None
    vector_index: object | None = None
    semantic_provider: object | None = None
    repair_scheduler: object | None = None
    metric_recorder: object | None = None


def build_phase6_recall_dependencies(
    engine: Engine,
    *,
    owner_id: str,
    database_identity: str,
    flags: MemoryV2FeatureFlags,
    settings: Settings,
) -> Phase6RecallDependencies:
    """Construct adapters only for explicitly enabled owner-scoped Phase 6 reads."""
    if not flags.owner_is_enabled(owner_id):
        return Phase6RecallDependencies()
    fts = SqliteMemoryV2FtsIndex(engine) if flags.fts_index_enabled else None
    if not flags.semantic_recall_enabled:
        return Phase6RecallDependencies(fts_index=fts)
    if settings.memory_v2_embedding_provider != "ollama":
        return Phase6RecallDependencies(fts_index=fts)
    raw_provider = OllamaEmbeddingProvider(
        model_name=settings.memory_v2_embedding_model,
        base_url=settings.ollama_url,
        timeout=settings.memory_v2_query_embedding_timeout_seconds,
    )
    provider = ValidatedMemoryV2EmbeddingProvider(
        raw_provider,
        dimension=settings.memory_v2_embedding_dimension,
        provider_version=settings.memory_v2_embedding_version,
        cooldown_seconds=settings.memory_v2_provider_cooldown_seconds,
    )
    vector = SqliteMemoryV2VectorIndex(engine)
    metrics = MemoryV2DerivedMetrics(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
    )
    processor = MemoryV2OutboxProcessor(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
        fts_index=fts,
        vector_index=vector,
        embedding_provider=provider,
        retry_policy=RetryPolicy(
            maximum_attempts=settings.memory_v2_retry_max_attempts,
            dead_letter_threshold=settings.memory_v2_dead_letter_threshold,
            base_delay_seconds=settings.memory_v2_retry_base_seconds,
            maximum_delay_seconds=settings.memory_v2_retry_max_seconds,
            jitter_seconds=settings.memory_v2_retry_jitter_seconds,
            lease_seconds=settings.memory_v2_worker_lease_seconds,
            batch_size=settings.memory_v2_worker_batch_size,
        ),
    )
    return Phase6RecallDependencies(
        fts_index=fts,
        vector_index=vector,
        semantic_provider=provider,
        repair_scheduler=processor.schedule_repair,
        metric_recorder=metrics.record,
    )
