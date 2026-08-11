"""Optional semantic and lexical recall wiring with no eager provider calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.db.session import build_engine
from app.services.embeddings import (
    OllamaEmbeddingProvider,
    ValidatedMemoryEmbeddingProvider,
)
from app.services.memory.index_contracts import RetryPolicy
from app.services.memory.indexes import SqliteMemoryFtsIndex, SqliteMemoryVectorIndex
from app.services.memory.metrics import MemoryDerivedMetrics
from app.services.memory.outbox import MemoryOutboxProcessor
from app.services.memory.settings import MemorySettings

_LOG = logging.getLogger("neo.memory.runtime")


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


def build_semantic_duplicate_finder(
    *,
    database_url: str,
    owner_id: str,
    database_identity: str,
    flags: MemorySettings,
    settings: Settings,
):
    """Build a callable that finds an existing memory meaning the same thing.

    Exact and normalized text comparison cannot tell that "simple 25-minute
    sessions with perspective steps" and "simple 25-minute practice sessions
    with perspective steps" are one preference, so a restatement in different
    words was stored as a second record and a later delete had two targets.
    The vector index already holds an embedding of every record's display text,
    so identity costs one embedding of the candidate and one search.

    Returns ``None`` when semantic recall is switched off or the provider is
    unavailable: failing to notice a duplicate must never block a write.
    """

    if not flags.semantic_recall_enabled or not flags.owner_is_enabled(owner_id):
        return None

    def find(text: str, allowed: frozenset[UUID], *, threshold: float) -> UUID | None:
        if not text.strip() or not allowed:
            return None
        engine = build_engine(database_url)
        try:
            dependencies = build_memory_recall_dependencies(
                engine,
                owner_id=owner_id,
                database_identity=database_identity,
                flags=flags,
                settings=settings,
            )
            provider = dependencies.semantic_provider
            index = dependencies.vector_index
            if provider is None or index is None:
                return None
            # Deliberately no pre-emptive health check.  The shared provider
            # reports unhealthy for a cooldown window after any recent timeout,
            # including one from an unrelated recall on a busy turn, and skipping
            # on that signal silently let a duplicate through while embedding was
            # in fact working.  Attempt the call and let the failure path below
            # decide, since a genuine failure costs one timeout and no data.
            vector = provider.embed(text)
            best: tuple[UUID, float] | None = None
            for hit in index.search(vector, owner_id, len(allowed) + 8):
                memory_id = getattr(hit, "memory_id", None)
                score = float(getattr(hit, "score", 0.0))
                if memory_id in allowed and (best is None or score > best[1]):
                    best = (memory_id, score)
            if best is not None and best[1] >= threshold:
                return best[0]
            _LOG.warning(
                "semantic_duplicate_miss best=%s threshold=%.3f comparable=%d",
                f"{best[1]:.4f}" if best else "none",
                threshold,
                len(allowed),
            )
            return None
        except Exception:
            _LOG.exception("semantic_duplicate_check_failed")
            return None
        finally:
            engine.dispose()

    return find


def drain_memory_outbox(
    engine: Engine,
    *,
    owner_id: str,
    database_identity: str,
    flags: MemorySettings,
    settings: Settings,
    worker_id: str = "neo-inline-index-worker",
    maximum_batches: int = 8,
) -> int:
    """Build the derived FTS and vector indexes for work already written.

    Writing a memory only enqueues its indexing work in the outbox.  Draining
    that queue lived solely in ``scripts/memory_index_worker.py``, which nothing
    runs in the deployed application, so ``memory_fts_documents`` and
    ``memory_vector_points`` stayed empty forever and semantic recall had
    nothing to search.  Recall then depended entirely on literal word overlap,
    which is why a memory stored as "V60 dripper" was unreachable from "what
    tools do i use to make coffee".

    This is bounded and best effort: it returns the number of completed targets
    and never raises into the caller, because failing to index must not fail the
    write that produced the work.
    """

    dependencies = build_memory_recall_dependencies(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
        flags=flags,
        settings=settings,
    )
    if dependencies.fts_index is None and dependencies.vector_index is None:
        return 0
    processor = MemoryOutboxProcessor(
        engine,
        owner_id=owner_id,
        database_identity=database_identity,
        fts_index=dependencies.fts_index,
        vector_index=dependencies.vector_index,
        embedding_provider=dependencies.semantic_provider,
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
    completed = 0
    for _ in range(max(1, maximum_batches)):
        batch = processor.lease_batch(worker_id=worker_id)
        if not batch.leases:
            break
        for result in processor.process_batch(batch):
            completed += len(result.completed_targets)
    return completed
