#!/usr/bin/env python3
"""Run the owner-bound memory-v2 derived-index outbox worker."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.memory_v2_migrations import memory_v2_migration_state
from app.db.session import build_engine
from app.services.embeddings import OllamaEmbeddingProvider, ValidatedMemoryV2EmbeddingProvider
from app.services.memory_v2.feature_flags import MemoryV2FeatureFlags
from app.services.memory_v2.indexes import SqliteMemoryV2FtsIndex, SqliteMemoryV2VectorIndex
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.phase6_contracts import (
    DerivedTarget,
    DerivedTargetState,
    RetryPolicy,
)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _lease_seconds(value: str) -> int:
    parsed = int(value)
    if parsed < 5:
        raise argparse.ArgumentTypeError("lease duration must be at least 5 seconds")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be greater than 0")
    return parsed


def _worker_id(value: str) -> str:
    parsed = value.strip()
    if not parsed or len(parsed) > 120:
        raise argparse.ArgumentTypeError("worker ID must contain 1 to 120 characters")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=_positive_int)
    parser.add_argument("--worker-id", type=_worker_id, default="memory-v2-index-worker")
    parser.add_argument("--poll-interval", type=_positive_float)
    parser.add_argument("--lease-seconds", type=_lease_seconds)
    parser.add_argument("--max-attempts", type=_positive_int)
    parser.add_argument("--owner-id")
    parser.add_argument("--database-url")
    parser.add_argument("--database-identity")
    parser.add_argument("--disposable-maintenance", action="store_true")
    parser.add_argument("--disposable-root")
    return parser


def _disposable_path(database_url: str, root: str) -> Path:
    if not database_url.startswith("sqlite:///") or not root:
        raise RuntimeError("disposable_sqlite_database_and_root_required")
    path = Path(database_url.removeprefix("sqlite:///")).expanduser().resolve()
    allowed = Path(root).expanduser().resolve()
    if path == allowed or allowed not in path.parents:
        raise RuntimeError("database_outside_disposable_root")
    return path


def _binding(args, engine, flags):
    supplied = any((args.owner_id, args.database_url, args.database_identity))
    if supplied and not args.disposable_maintenance:
        raise RuntimeError("manual_owner_or_database_requires_disposable_maintenance")
    if args.disposable_maintenance:
        if not all((args.owner_id, args.database_url, args.database_identity)):
            raise RuntimeError("disposable_owner_database_binding_required")
        _disposable_path(args.database_url, args.disposable_root or "")
        return str(UUID(args.owner_id)), args.database_identity, True, True
    if not flags.outbox_worker_enabled:
        raise RuntimeError("memory_v2_outbox_worker_disabled")
    state = memory_v2_migration_state(engine)
    if state.owner_id is None or state.database_identity is None:
        raise RuntimeError("memory_v2_owner_binding_missing")
    if state.owner_id not in flags.enabled_owner_ids:
        raise RuntimeError("memory_v2_worker_owner_not_enabled")
    return (
        state.owner_id,
        state.database_identity,
        flags.fts_index_enabled,
        flags.vector_index_enabled,
    )


def main() -> int:
    args = _parser().parse_args()
    settings = get_settings()
    flags = MemoryV2FeatureFlags.from_settings(settings)
    database_url = args.database_url or settings.database_url
    engine = build_engine(database_url)
    owner_id, identity, enable_fts, enable_vector = _binding(args, engine, flags)
    fts = SqliteMemoryV2FtsIndex(engine) if enable_fts else None
    vector = SqliteMemoryV2VectorIndex(engine) if enable_vector else None
    provider = None
    if enable_vector:
        if settings.memory_v2_embedding_provider != "ollama":
            raise RuntimeError("unsupported_memory_v2_embedding_provider")
        provider = ValidatedMemoryV2EmbeddingProvider(
            OllamaEmbeddingProvider(
                model_name=settings.memory_v2_embedding_model,
                base_url=settings.ollama_url,
                timeout=settings.memory_v2_index_embedding_timeout_seconds,
            ),
            dimension=settings.memory_v2_embedding_dimension,
            provider_version=settings.memory_v2_embedding_version,
            cooldown_seconds=settings.memory_v2_provider_cooldown_seconds,
        )
    retry = RetryPolicy(
        maximum_attempts=args.max_attempts or settings.memory_v2_retry_max_attempts,
        dead_letter_threshold=settings.memory_v2_dead_letter_threshold,
        base_delay_seconds=settings.memory_v2_retry_base_seconds,
        maximum_delay_seconds=settings.memory_v2_retry_max_seconds,
        jitter_seconds=settings.memory_v2_retry_jitter_seconds,
        lease_seconds=args.lease_seconds or settings.memory_v2_worker_lease_seconds,
        batch_size=args.batch_size or settings.memory_v2_worker_batch_size,
    )
    processor = MemoryV2OutboxProcessor(
        engine,
        owner_id=owner_id,
        database_identity=identity,
        fts_index=fts,
        vector_index=vector,
        embedding_provider=provider,
        retry_policy=retry,
    )
    poll = args.poll_interval or settings.memory_v2_worker_poll_seconds
    while True:
        batch = processor.lease_batch(
            worker_id=args.worker_id,
            batch_size=args.batch_size,
            lease_seconds=args.lease_seconds,
        )
        results = processor.process_batch(batch)
        completed = sum(len(item.completed_targets) for item in results)
        retryable = sum(len(item.retryable_targets) for item in results)
        dead = sum(len(item.dead_lettered_targets) for item in results)
        fts_completed = sum(
            diagnostic.target is DerivedTarget.FTS
            and diagnostic.operation == "upsert"
            and diagnostic.to_state is DerivedTargetState.CURRENT
            for item in results
            for diagnostic in item.diagnostics
        )
        vector_completed = sum(
            diagnostic.target is DerivedTarget.VECTOR
            and diagnostic.operation == "upsert"
            and diagnostic.to_state is DerivedTargetState.CURRENT
            for item in results
            for diagnostic in item.diagnostics
        )
        vector_failed = sum(
            target is DerivedTarget.VECTOR
            for item in results
            for target in (*item.retryable_targets, *item.dead_lettered_targets)
        )
        print(f"leased={len(batch.leases)}")
        print(f"completed={completed}")
        print(f"retryable_failed={retryable}")
        print(f"dead_lettered={dead}")
        print(f"fts_upserted={fts_completed}")
        print(f"vector_upserted={vector_completed}")
        print(f"vector_failed={vector_failed}")
        print("canonical_mutations=0")
        if args.once:
            return 0
        if not batch.leases:
            time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
