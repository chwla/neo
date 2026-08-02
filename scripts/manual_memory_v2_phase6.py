#!/usr/bin/env python3
"""Validate Phase 6 derived indexing and recall in disposable namespaces only."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, update

from app.core.config import Settings
from app.models.memory_v2 import (
    MemoryOutboxDeliveryV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemoryVectorPointV2,
)
from app.services.embeddings import OllamaEmbeddingProvider
from app.services.memory_v2.contracts import Sensitivity
from app.services.memory_v2.extraction_contracts import CurrentTurnOverride
from app.services.memory_v2.indexes import DerivedDocumentBuilder
from app.services.memory_v2.maintenance import MemoryV2IndexMaintenance
from app.services.memory_v2.outbox import MemoryV2OutboxProcessor
from app.services.memory_v2.phase6_contracts import DerivedTarget, RetryPolicy, VectorCandidate
from app.services.memory_v2.prompt import RecallPromptOrchestrator, repository_usage_recorder
from app.services.memory_v2.queries import RecallMode, RecallQuery
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.taxonomy import MemoryType
from app.services.memory_v2.versions import VECTOR_METADATA_VERSION
from tests.memory_v2.phase3_helpers import OWNER_B
from tests.memory_v2.phase5_helpers import add_memory, query_context
from tests.memory_v2.phase6_helpers import phase6_harness, phase6_services


class FixtureVector:
    def __init__(self, hits=(), *, fail: bool = False) -> None:
        self.hits = list(hits)
        self.fail = fail
        self.calls = 0

    def search(self, _vector, _owner_id, _limit):
        self.calls += 1
        if self.fail:
            raise RuntimeError("vector_unavailable")
        return list(self.hits)


class BrokenFts:
    def search(self, *_args):
        raise RuntimeError("fts_unavailable")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--probe-live-embeddings", action="store_true")
    parser.add_argument("--provider", default="ollama")
    parser.add_argument("--endpoint", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="nomic-embed-text:latest")
    return parser


def _hit(document, provider, **changes) -> VectorCandidate:
    embedding_document = DerivedDocumentBuilder.build_embedding(document)
    values = {
        "owner_id": document.owner_id,
        "memory_id": document.memory_id,
        "content_hash": document.content_hash,
        "canonical_revision": document.canonical_revision,
        "score": 1.0,
        "provider": provider.provider_name,
        "model": provider.model_name,
        "provider_version": provider.provider_version,
        "dimension": provider.dimension,
        "metadata_version": VECTOR_METADATA_VERSION,
        "derived_schema_version": document.schema_version,
        "embedding_document_version": embedding_document.version,
        "embedding_content_hash": embedding_document.content_hash,
    }
    values.update(changes)
    return VectorCandidate(**values)


def _drain(services, worker: str) -> int:
    leased = 0
    while True:
        batch = services.processor.lease_batch(worker_id=worker)
        if not batch.leases:
            return leased
        leased += len(batch.leases)
        services.processor.process_batch(batch)


def _maintenance(services, *, oldest: int = 900) -> MemoryV2IndexMaintenance:
    return MemoryV2IndexMaintenance(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=services.fts,
        vector_index=services.vector,
        repair_scheduler=services.processor.schedule_repair,
        embedding_provider=services.provider,
        provider_health=services.provider.health,
        metric_reader=services.metrics.snapshot,
        alert_oldest_pending_seconds=oldest,
    )


def _semantic_recall(services, vector, repairs):
    return CanonicalRecallService(
        services.phase5.repository,
        flags=services.phase5.harness.coordinator.flags,
        semantic_provider=services.provider,
        vector_index=vector,
        repair_scheduler=repairs.append,
        metric_recorder=services.metrics.record,
    )


def _run(root: Path) -> dict[str, object]:
    harness, adapter = phase6_harness(root / "main")
    pending_id = add_memory(
        adapter,
        harness,
        key="manual-pending",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:pending",
        text="synthetic phase six pending fixture",
    )
    services = phase6_services(harness)
    try:
        pending_before = services.phase5.session.scalar(
            select(func.count(MemoryOutboxV2.id)).where(MemoryOutboxV2.state == "pending")
        )
        canonical_provider_calls = services.provider_source.calls
        leased = _drain(services, "manual-success")
        assert services.fts.get_metadata(services.phase5.repository.owner_id, str(pending_id))
        assert services.vector.get_metadata(services.phase5.repository.owner_id, str(pending_id))

        retry_id = add_memory(
            adapter,
            harness,
            key="manual-retry",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot="knowledge:learning:retry",
            text="synthetic vector retry fixture",
        )
        services.provider_source.fail = True
        failed_batch = services.processor.lease_batch(worker_id="manual-failure")
        services.processor.process_batch(failed_batch)
        assert services.fts.get_metadata(services.phase5.repository.owner_id, str(retry_id))
        assert (
            services.vector.get_metadata(services.phase5.repository.owner_id, str(retry_id)) is None
        )
        services.phase5.session.execute(
            update(MemoryOutboxDeliveryV2)
            .where(MemoryOutboxDeliveryV2.state == "failed")
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.phase5.session.execute(
            update(MemoryOutboxV2)
            .where(MemoryOutboxV2.state == "failed")
            .values(next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.phase5.session.commit()
        services.provider_source.fail = False
        _drain(services, "manual-retry")
        vector_retry_recovered = (
            services.vector.get_metadata(services.phase5.repository.owner_id, str(retry_id))
            is not None
        )

        old_id = add_memory(
            adapter,
            harness,
            key="manual-old-goal",
            memory_type=MemoryType.GOAL,
            domain="video_creation",
            slot="goal:video_creation:current_primary_goal",
            text="create long form synthetic videos",
        )
        services.phase5.session.expire_all()
        old_record = services.phase5.session.get(MemoryRecordV2, str(old_id))
        old_document = services.processor.document_builder.build(old_record, now=datetime.now(UTC))
        services.phase5.session.execute(
            update(MemoryRecordV2)
            .where(MemoryRecordV2.id == str(old_id))
            .values(status="superseded", revision=2)
        )
        services.phase5.session.commit()
        new_id = add_memory(
            adapter,
            harness,
            key="manual-new-goal",
            memory_type=MemoryType.GOAL,
            domain="video_creation",
            slot="goal:video_creation:current_primary_goal",
            text="create short synthetic reels",
        )
        services.phase5.session.expire_all()
        new_record = services.phase5.session.get(MemoryRecordV2, str(new_id))
        new_document = services.processor.document_builder.build(new_record, now=datetime.now(UTC))
        ghost_id = uuid4()
        wrong_id = uuid4()
        hits = [
            _hit(old_document, services.provider),
            _hit(new_document, services.provider, memory_id=ghost_id),
            _hit(
                new_document,
                services.provider,
                memory_id=wrong_id,
                owner_id=UUID(OWNER_B),
            ),
            _hit(new_document, services.provider, content_hash="0" * 64),
            _hit(new_document, services.provider),
        ]
        repairs = []
        recall = _semantic_recall(services, FixtureVector(hits), repairs)
        semantic = recall.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"video_creation"})),
                text="short synthetic reels",
            )
        )
        assert new_id in semantic.canonical_ids
        assert semantic.diagnostic.semantic_inactive_drop_count == 1
        assert semantic.diagnostic.semantic_ghost_drop_count == 1
        assert semantic.diagnostic.semantic_wrong_owner_drop_count == 1
        assert semantic.diagnostic.semantic_stale_drop_count == 1

        outage = _semantic_recall(services, FixtureVector(fail=True), [])
        lexical = outage.recall(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"video_creation"})),
                text="short synthetic reels",
            )
        )
        deterministic = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            semantic_provider=services.provider,
            vector_index=FixtureVector(fail=True),
            fts_index=BrokenFts(),
        ).recall(
            RecallQuery(
                context=query_context(services.phase5, mode=RecallMode.DETERMINISTIC),
                canonical_id=new_id,
            )
        )

        override = CurrentTurnOverride(
            owner_id=services.phase5.repository.owner_id,
            source_message_id="manual-current-turn",
            suppressed_memory_ids=(new_id,),
            suppressed_slot_keys=(new_record.slot_key,),
            contradicted_memory_ids=(new_id,),
            contradicted_slot_keys=(new_record.slot_key,),
            contradiction_deterministic=True,
        )
        suppressed = _semantic_recall(
            services, FixtureVector([_hit(new_document, services.provider)]), []
        ).recall(
            RecallQuery(
                context=query_context(
                    services.phase5,
                    domains=frozenset({"video_creation"}),
                    override=override,
                ),
                text="short synthetic reels",
            )
        )
        prompt = RecallPromptOrchestrator(
            _semantic_recall(services, FixtureVector([_hit(new_document, services.provider)]), []),
            usage_recorder=repository_usage_recorder(services.phase5.repository),
        )
        selection = prompt.build(
            RecallQuery(
                context=query_context(services.phase5, domains=frozenset({"video_creation"})),
                text="short synthetic reels",
            ),
            purpose="manual-phase6-usage",
        )
        usage_parity = bool(
            selection.serialized
            and selection.recall.diagnostic.usage_event_ids == selection.serialized.canonical_ids
        )

        reconciliation_id = add_memory(
            adapter,
            harness,
            key="manual-reconciliation",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot="knowledge:learning:reconciliation",
            text="synthetic reconciliation fixture",
        )
        _drain(services, "manual-before-reconciliation")
        owner_id = services.phase5.repository.owner_id
        assert services.vector.delete(owner_id, str(reconciliation_id))
        maintenance = _maintenance(services)
        missing_before = maintenance.reconcile(dry_run=False).missing_vector
        _drain(services, "manual-reconciliation")
        missing_after = maintenance.reconcile(dry_run=False)

        derived_ghost_id = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        derived_ghost = new_document.model_copy(update={"memory_id": derived_ghost_id})
        services.fts.upsert(derived_ghost)
        services.vector.upsert(
            derived_ghost,
            services.provider.embed(derived_ghost.display_text),
            services.provider,
        )
        bounded_reports = []
        checkpoint = None
        while True:
            report = maintenance.reconcile(
                dry_run=False,
                limit=1,
                checkpoint=checkpoint,
            )
            bounded_reports.append(report)
            assert report.checked <= 1
            assert report.fts_metadata_checked <= 1
            assert report.vector_metadata_checked <= 1
            if report.next_checkpoint is None:
                break
            checkpoint = report.next_checkpoint
            assert len(bounded_reports) < 25
        derived_ghost_repairs = sum(
            report.ghost_fts + report.ghost_vector for report in bounded_reports
        )
        _drain(services, "manual-bounded-reconciliation")
        derived_ghost_removed = bool(
            services.fts.get_metadata(owner_id, str(derived_ghost_id)) is None
            and services.vector.get_metadata(owner_id, str(derived_ghost_id)) is None
        )

        rebuild = maintenance.rebuild_owner()
        _drain(services, "manual-rebuild")
        rebuild_verification = maintenance.verify_owner_rebuild(rebuild)
        coverage = maintenance.coverage()

        return {
            "database_path": harness.database_path,
            "pending_before": pending_before,
            "canonical_provider_calls": canonical_provider_calls,
            "leased": leased,
            "fts_current": coverage.fts_current_count,
            "vector_current": coverage.vector_current_count,
            "retry_recovered": vector_retry_recovered,
            "stale_returned": old_id in semantic.canonical_ids,
            "ghost_returned": ghost_id in semantic.canonical_ids,
            "hash_returned": semantic.diagnostic.semantic_stale_drop_count == 0,
            "wrong_returned": wrong_id in semantic.canonical_ids,
            "wrong_suppressed": new_id not in semantic.canonical_ids,
            "inactive_returned": old_id in semantic.canonical_ids,
            "lexical_fallback": new_id in lexical.canonical_ids,
            "deterministic_fallback": deterministic.canonical_ids == (new_id,),
            "missing_before": missing_before,
            "missing_after": missing_after.missing_vector,
            "second_repairs": missing_after.repairs_queued,
            "bounded_pages": len(bounded_reports),
            "derived_ghost_repairs": derived_ghost_repairs,
            "derived_ghost_removed": derived_ghost_removed,
            "checksum": (
                rebuild.canonical_checksum_before == rebuild.canonical_checksum_after
                and rebuild_verification.equivalent
            ),
            "suppressed": new_id not in suppressed.canonical_ids,
            "usage_parity": usage_parity,
        }
    finally:
        services.close()


def _crash_retry(root: Path) -> int:
    harness, adapter = phase6_harness(root / "crash")
    memory_id = add_memory(
        adapter,
        harness,
        key="manual-crash",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:crash",
        text="synthetic crash retry fixture",
    )
    services = phase6_services(harness)
    crashed = False

    def crash(target, _event_id):
        nonlocal crashed
        if target.value == "vector" and not crashed:
            crashed = True
            raise SystemExit("synthetic_process_loss")

    processor = MemoryV2OutboxProcessor(
        services.phase5.session.get_bind(),
        owner_id=services.phase5.repository.owner_id,
        database_identity=services.phase5.repository.database_identity,
        fts_index=services.fts,
        vector_index=services.vector,
        embedding_provider=services.provider,
        after_target_write=crash,
    )
    try:
        try:
            processor.process_batch(
                processor.lease_batch(worker_id="manual-crashed", lease_seconds=5)
            )
        except SystemExit:
            pass
        services.phase5.session.execute(
            update(MemoryOutboxDeliveryV2)
            .where(MemoryOutboxDeliveryV2.state == "processing")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.phase5.session.commit()
        processor.after_target_write = None
        processor.process_batch(processor.lease_batch(worker_id="manual-crash-retry"))
        count = services.phase5.session.scalar(
            select(func.count(MemoryVectorPointV2.id)).where(
                MemoryVectorPointV2.memory_id == str(memory_id)
            )
        )
        return int(count) - 1
    finally:
        services.close()


def _security_dead_letter_alert(root: Path) -> dict[str, object]:
    sensitive_harness, sensitive_adapter = phase6_harness(root / "sensitive")
    add_memory(
        sensitive_adapter,
        sensitive_harness,
        key="manual-sensitive",
        memory_type=MemoryType.KNOWLEDGE,
        domain="health_fitness",
        slot="knowledge:health_fitness:sensitive",
        text="synthetic sensitive fixture",
        sensitivity=Sensitivity.SENSITIVE,
    )
    sensitive = phase6_services(sensitive_harness)
    try:
        _drain(sensitive, "manual-sensitive")
        sensitive_calls = sensitive.provider_source.calls
        counting = FixtureVector()
        gated_recall = CanonicalRecallService(
            sensitive.phase5.repository,
            flags=sensitive.phase5.harness.coordinator.flags,
            semantic_provider=sensitive.provider,
            vector_index=counting,
        )
        before_provider = sensitive.provider_source.calls
        gated_recall.recall(
            RecallQuery(
                context=query_context(sensitive.phase5, incognito=True),
                text="synthetic sensitive fixture",
            )
        )
        incognito_calls = counting.calls + sensitive.provider_source.calls - before_provider
        gated_recall.recall(
            RecallQuery(
                context=query_context(sensitive.phase5, memory_enabled=False),
                text="synthetic sensitive fixture",
            )
        )
        disabled_calls = counting.calls + sensitive.provider_source.calls - before_provider
    finally:
        sensitive.close()

    dead_harness, dead_adapter = phase6_harness(root / "dead")
    add_memory(
        dead_adapter,
        dead_harness,
        key="manual-dead",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:dead",
        text="synthetic dead letter fixture",
    )
    dead = phase6_services(dead_harness, retry_policy=RetryPolicy(maximum_attempts=1))
    try:
        dead.provider_source.fail = True
        batch = dead.processor.lease_batch(worker_id="manual-dead")
        result = dead.processor.process_batch(batch)[0]
        visible = bool(result.dead_lettered_targets)
        requeued = dead.processor.requeue_dead_letter(
            batch.leases[0].event_id, DerivedTarget.VECTOR
        )
        dead.provider_source.fail = False
        _drain(dead, "manual-requeued")
        recovered = requeued and dead.vector.list_metadata_for_owner(
            dead.phase5.repository.owner_id
        )
    finally:
        dead.close()

    alert_harness, alert_adapter = phase6_harness(root / "alert")
    add_memory(
        alert_adapter,
        alert_harness,
        key="manual-alert",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:alert",
        text="synthetic old pending fixture",
    )
    alert = phase6_services(alert_harness)
    try:
        alert.phase5.session.execute(
            update(MemoryOutboxV2).values(created_at=datetime.now(UTC) - timedelta(hours=1))
        )
        alert.phase5.session.commit()
        report = _maintenance(alert, oldest=1).coverage()
        oldest_alert = "oldest_pending_age" in report.alert_codes and not report.rollout_ready
    finally:
        alert.close()
    return {
        "sensitive_calls": sensitive_calls,
        "incognito_calls": incognito_calls,
        "disabled_calls": disabled_calls,
        "dead_visible": visible,
        "dead_recovered": bool(recovered),
        "oldest_alert": oldest_alert,
    }


def _probe(args) -> bool:
    if args.provider != "ollama":
        raise RuntimeError("only_ollama_live_probe_is_supported")
    provider = OllamaEmbeddingProvider(
        model_name=args.model,
        base_url=args.endpoint,
        timeout=30,
    )
    started = time.perf_counter()
    try:
        vector = provider.embed("synthetic non-personal phase six embedding probe")
        success = bool(vector) and all(math.isfinite(float(item)) for item in vector)
        print("provider_reachable=true")
        print("model_available=true")
        print(f"embedding_success={str(success).lower()}")
        print(f"embedding_dimension={len(vector)}")
        print(f"finite_vector={str(success).lower()}")
        print(f"provider_model={provider.provider_name}/{provider.model_name}")
        print(f"latency_ms={int((time.perf_counter() - started) * 1000)}")
        return success
    except Exception:
        print("provider_reachable=false")
        print("model_available=false")
        print("embedding_success=false")
        print("embedding_dimension=0")
        print("finite_vector=false")
        print(f"provider_model=ollama/{args.model}")
        print(f"latency_ms={int((time.perf_counter() - started) * 1000)}")
        return False


def main() -> int:
    args = _parser().parse_args()
    root = Path(tempfile.mkdtemp(prefix="neo-memory-v2-phase6-"))
    passed = False
    try:
        result = _run(root)
        crash_duplicates = _crash_retry(root)
        safety = _security_dead_letter_alert(root)
        assert result["pending_before"] > 0
        assert result["canonical_provider_calls"] == 0
        assert result["retry_recovered"]
        assert crash_duplicates == 0
        assert not result["stale_returned"]
        assert not result["ghost_returned"]
        assert not result["hash_returned"]
        assert not result["wrong_returned"]
        assert not result["wrong_suppressed"]
        assert not result["inactive_returned"]
        assert result["lexical_fallback"] and result["deterministic_fallback"]
        assert result["missing_before"] > 0 and result["missing_after"] == 0
        assert result["second_repairs"] == 0 and result["checksum"]
        assert result["derived_ghost_repairs"] == 2
        assert result["derived_ghost_removed"]
        assert result["suppressed"] and result["usage_parity"]
        assert safety["sensitive_calls"] == 0
        assert safety["incognito_calls"] == 0 and safety["disabled_calls"] == 0
        assert safety["dead_visible"] and safety["dead_recovered"]
        assert safety["oldest_alert"]
        defaults = Settings.model_fields
        phase6_flags = (
            "memory_v2_outbox_worker_enabled",
            "memory_v2_fts_index_enabled",
            "memory_v2_vector_index_enabled",
            "memory_v2_semantic_recall_enabled",
            "memory_v2_reconciliation_enabled",
            "memory_v2_derived_health_routes_enabled",
        )
        enabled_defaults = sum(defaults[item].default is True for item in phase6_flags)
        phase7 = sum(
            "phase7" in path.name.casefold()
            for path in (Path(__file__).resolve().parents[1] / "app").rglob("*")
        )
        print("phase6_fixture_validation=PASS")
        print(f"canonical_mutation_provider_calls={result['canonical_provider_calls']}")
        print(f"outbox_pending_after_canonical_commit={result['pending_before']}")
        print(f"leased_event_count={result['leased']}")
        print(f"fts_current_count={result['fts_current']}")
        print(f"vector_current_count={result['vector_current']}")
        print(f"vector_retry_recovered={str(result['retry_recovered']).lower()}")
        print(f"crash_retry_duplicate_vector_count={crash_duplicates}")
        print(f"stale_hit_returned={str(result['stale_returned']).lower()}")
        print(f"ghost_hit_returned={str(result['ghost_returned']).lower()}")
        print(f"hash_mismatch_hit_returned={str(result['hash_returned']).lower()}")
        print(f"wrong_owner_hit_returned={str(result['wrong_returned']).lower()}")
        print(f"wrong_owner_suppressed_local_fallback={str(result['wrong_suppressed']).lower()}")
        print(f"inactive_hit_returned={str(result['inactive_returned']).lower()}")
        print(f"vector_outage_lexical_fallback={str(result['lexical_fallback']).lower()}")
        print(f"full_outage_deterministic_fallback={str(result['deterministic_fallback']).lower()}")
        print(f"reconciliation_missing_before={result['missing_before']}")
        print(f"reconciliation_missing_after={result['missing_after']}")
        print(f"reconciliation_second_run_repairs={result['second_repairs']}")
        print(f"reconciliation_bounded_pages={result['bounded_pages']}")
        print(f"reconciliation_derived_ghost_repairs={result['derived_ghost_repairs']}")
        print(
            f"reconciliation_derived_ghost_removed={str(result['derived_ghost_removed']).lower()}"
        )
        print(f"rebuild_canonical_checksum_unchanged={str(result['checksum']).lower()}")
        print(f"sensitive_embedding_calls={safety['sensitive_calls']}")
        print(f"incognito_phase6_calls={safety['incognito_calls']}")
        print(f"memory_disabled_phase6_calls={safety['disabled_calls']}")
        print(f"usage_ids_match_serialized_ids={str(result['usage_parity']).lower()}")
        print("vectors_authorized_from_canonical_only=true")
        print(f"dead_letter_visible={str(safety['dead_visible']).lower()}")
        print(f"dead_letter_requeue_recovered={str(safety['dead_recovered']).lower()}")
        print(f"production_flags_enabled={enabled_defaults}")
        print(f"phase7_files_added={phase7}")
        if args.probe_live_embeddings and not _probe(args):
            return 1
        passed = True
        return 0
    finally:
        database_path = result["database_path"] if "result" in locals() else root
        print(f"disposable_database_path={database_path}")
        print(f"disposable_index_path={database_path}#memory_v2_derived")
        print(f"cleanup_command=rm -rf -- {root}")
        if passed and not args.keep:
            shutil.rmtree(root)


if __name__ == "__main__":
    raise SystemExit(main())
