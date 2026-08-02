from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from app.core.config import Settings
from app.models.memory_v2 import (
    MemoryFtsDocumentV2,
    MemoryOutboxDeliveryV2,
    MemoryOutboxV2,
    MemoryRecordV2,
    MemoryVectorPointV2,
)
from app.services.memory_v2.maintenance import (
    MemoryV2IndexMaintenance,
    PrivilegedGlobalMemoryV2Maintenance,
)
from app.services.memory_v2.phase6_contracts import (
    DerivedMetricCode,
    DerivedTarget,
    IndexRepairRequest,
    ProviderHealth,
)
from app.services.memory_v2.queries import RecallQuery
from app.services.memory_v2.recall import CanonicalRecallService
from app.services.memory_v2.taxonomy import MemoryType
from tests.memory_v2.phase5_helpers import add_memory, query_context
from tests.memory_v2.phase6_helpers import phase6_harness, phase6_services


def _drain(services, worker_id: str = "reconciliation-worker") -> None:
    while True:
        batch = services.processor.lease_batch(worker_id=worker_id)
        if not batch.leases:
            return
        services.processor.process_batch(batch)


def _maintenance(services) -> MemoryV2IndexMaintenance:
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
    )


def test_missing_vector_reconciliation_repairs_once_and_second_run_is_clean(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="phase6-reconcile-missing",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:reconciliation",
        text="practice retrieval with bounded fixtures",
    )
    services = phase6_services(harness)
    try:
        _drain(services)
        owner_id = services.phase5.repository.owner_id
        assert services.vector.delete(owner_id, str(memory_id))
        assert services.vector.get_metadata(owner_id, str(memory_id)) is None
        assert services.fts.get_metadata(owner_id, str(memory_id)) is not None

        maintenance = _maintenance(services)
        missing_coverage = maintenance.coverage()
        assert missing_coverage.fts_current_count == 1
        assert missing_coverage.vector_current_count == 0
        assert missing_coverage.vector_missing_count == 1
        assert missing_coverage.degraded
        assert not missing_coverage.rollout_ready
        first = maintenance.reconcile(dry_run=False)
        assert first.missing_vector == 1
        assert first.missing_fts == 0
        assert first.done_missing_derived == 1
        assert first.repairs_queued == 1
        _drain(services)
        assert services.vector.get_metadata(owner_id, str(memory_id)) is not None
        assert services.fts.get_metadata(owner_id, str(memory_id)) is not None

        second = maintenance.reconcile(dry_run=False)
        assert second.missing_vector == 0
        assert second.missing_fts == 0
        assert second.stale_vector == 0
        assert second.stale_fts == 0
        assert second.repairs_queued == 0
    finally:
        services.close()


def test_reconciliation_classifies_inactive_expired_ghost_and_model_drift(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    inactive_id = add_memory(
        adapter,
        harness,
        key="phase6-reconcile-inactive",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:inactive-index",
        text="synthetic inactive indexed fixture",
    )
    expired_id = add_memory(
        adapter,
        harness,
        key="phase6-reconcile-expired",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:expired-index",
        text="synthetic expired indexed fixture",
    )
    model_id = add_memory(
        adapter,
        harness,
        key="phase6-reconcile-model",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:model-index",
        text="synthetic embedding model fixture",
    )
    services = phase6_services(harness)
    try:
        _drain(services)
        model_record = services.phase5.session.get(MemoryRecordV2, str(model_id))
        model_document = services.processor.document_builder.build(
            model_record, now=datetime.now(UTC)
        )
        ghost_document = model_document.model_copy(update={"memory_id": uuid4()})
        services.fts.upsert(ghost_document)
        services.vector.upsert(
            ghost_document,
            services.provider.embed(ghost_document.display_text),
            services.provider,
        )
        services.phase5.session.execute(
            update(MemoryRecordV2)
            .where(MemoryRecordV2.id == str(inactive_id))
            .values(status="archived")
        )
        services.phase5.session.execute(
            update(MemoryRecordV2)
            .where(MemoryRecordV2.id == str(expired_id))
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        services.phase5.session.execute(
            update(MemoryFtsDocumentV2)
            .where(MemoryFtsDocumentV2.memory_id == str(model_id))
            .values(derived_schema_version="obsolete-derived-schema")
        )
        services.phase5.session.execute(
            update(MemoryVectorPointV2)
            .where(MemoryVectorPointV2.memory_id == str(model_id))
            .values(
                model="obsolete-fixture-model",
                embedding_document_version="obsolete-embedding-document",
            )
        )
        services.phase5.session.commit()

        maintenance = _maintenance(services)
        first = maintenance.reconcile()
        assert not first.dry_run
        assert first.inactive_indexed == 2
        assert first.expired_indexed == 2
        assert first.ghost_fts == 1
        assert first.ghost_vector == 1
        assert first.stale_fts == 1
        assert first.wrong_model_vector == 1
        assert first.repairs_queued == 8
        _drain(services, "classification-repair-worker")

        second = maintenance.reconcile()
        assert second.inactive_indexed == 0
        assert second.expired_indexed == 0
        assert second.ghost_fts == 0
        assert second.ghost_vector == 0
        assert second.stale_fts == 0
        assert second.wrong_model_vector == 0
        assert second.repairs_queued == 0
        metadata = services.vector.get_metadata(services.phase5.repository.owner_id, str(model_id))
        assert metadata["model"] == services.provider.model_name
    finally:
        services.close()


def test_reconciliation_repairs_stale_hashes_and_detects_pending_current(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="phase6-reconcile-stale",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:stale-index",
        text="synthetic stale index fixture",
    )
    services = phase6_services(harness)
    try:
        _drain(services)
        services.phase5.session.execute(
            update(MemoryFtsDocumentV2)
            .where(MemoryFtsDocumentV2.memory_id == str(memory_id))
            .values(content_hash="0" * 64)
        )
        services.phase5.session.execute(
            update(MemoryVectorPointV2)
            .where(MemoryVectorPointV2.memory_id == str(memory_id))
            .values(content_hash="0" * 64)
        )
        services.phase5.session.commit()
        maintenance = _maintenance(services)
        stale = maintenance.reconcile()
        assert stale.stale_fts == stale.stale_vector == 1
        assert stale.repairs_queued == 2
        stale_coverage = maintenance.coverage()
        assert stale_coverage.fts_stale_count == 1
        assert stale_coverage.vector_stale_count == 1
        assert stale_coverage.fts_missing_count == 0
        assert stale_coverage.vector_missing_count == 0
        assert not stale_coverage.rollout_ready
        _drain(services, "stale-repair-worker")
        assert maintenance.reconcile().repairs_queued == 0

        delivery = services.phase5.session.scalar(
            select(MemoryOutboxDeliveryV2).where(MemoryOutboxDeliveryV2.target == "vector")
        )
        event = services.phase5.session.get(MemoryOutboxV2, delivery.event_id)
        delivery.state = "pending"
        delivery.completed_at = None
        event.state = "pending"
        event.completed_at = None
        services.phase5.session.commit()
        report = maintenance.reconcile(dry_run=True)
        assert report.pending_already_current == 1
        assert services.vector.delete(
            services.phase5.repository.owner_id,
            str(memory_id),
        )
        assert maintenance.reconcile(dry_run=True).pending_already_current == 0
    finally:
        services.close()


def test_done_target_specific_event_does_not_claim_unrequested_target_is_missing(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="phase6-done-target-specific",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:done-target-specific",
        text="synthetic target specific fixture",
    )
    services = phase6_services(harness)
    try:
        original = services.phase5.session.scalar(
            select(MemoryOutboxV2).where(MemoryOutboxV2.memory_id == str(memory_id))
        )
        services.phase5.session.delete(original)
        services.phase5.session.commit()
        services.processor.schedule_repair(
            IndexRepairRequest(
                owner_id=services.phase5.repository.owner_id,
                memory_id=memory_id,
                action="upsert",
                target=DerivedTarget.VECTOR,
                reason="target_specific_fixture",
            )
        )
        _drain(services, "target-specific-worker")
        assert services.vector.get_metadata(services.phase5.repository.owner_id, str(memory_id))
        assert (
            services.fts.get_metadata(services.phase5.repository.owner_id, str(memory_id)) is None
        )

        report = _maintenance(services).reconcile(dry_run=True)
        assert report.missing_fts == 1
        assert report.missing_vector == 0
        assert report.done_missing_derived == 0
    finally:
        services.close()


def test_owner_rebuild_preserves_canonical_state_and_recreates_indexes(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_ids = [
        add_memory(
            adapter,
            harness,
            key=f"phase6-rebuild-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:rebuild_{index}",
            text=f"synthetic rebuild fixture {index}",
        )
        for index in range(2)
    ]
    services = phase6_services(harness)
    try:
        _drain(services)
        recall = CanonicalRecallService(
            services.phase5.repository,
            flags=services.phase5.harness.coordinator.flags,
            fts_index=services.fts,
            semantic_provider=services.provider,
            vector_index=services.vector,
        )
        recall_query = RecallQuery(
            context=query_context(
                services.phase5,
                domains=frozenset({"learning"}),
            ),
            text="synthetic rebuild fixture",
        )
        before_recall = recall.recall(recall_query)
        before_ranked = tuple(
            (item.memory.canonical_id, round(item.score.total, 12)) for item in before_recall.items
        )
        before = {
            row.id: (row.revision, row.status)
            for row in services.phase5.session.scalars(select(MemoryRecordV2))
        }
        maintenance = _maintenance(services)
        result = maintenance.rebuild_owner(now=datetime.now(UTC))
        assert result.canonical_checksum_before == result.canonical_checksum_after
        assert result.canonical_mutations == 0
        assert result.queued == 2
        assert result.canonical_eligible_count == 2
        assert result.fts_cleared_count == 2
        assert result.vector_cleared_count == 2
        assert result.pending_target_count == 4
        pending_coverage = maintenance.coverage()
        assert pending_coverage.fts_current_count == 0
        assert pending_coverage.vector_current_count == 0
        assert pending_coverage.pending_outbox_count == 2
        _drain(services)
        services.phase5.session.expire_all()
        after = {
            row.id: (row.revision, row.status)
            for row in services.phase5.session.scalars(select(MemoryRecordV2))
        }
        assert after == before
        owner_id = services.phase5.repository.owner_id
        for memory_id in memory_ids:
            assert services.fts.get_metadata(owner_id, str(memory_id)) is not None
            assert services.vector.get_metadata(owner_id, str(memory_id)) is not None
        verification = maintenance.verify_owner_rebuild(result)
        assert verification.equivalent
        assert verification.fts_count == verification.canonical_eligible_count == 2
        assert verification.vector_count == 2
        assert verification.fts_missing_or_stale == 0
        assert verification.vector_missing_or_stale == 0
        after_recall = recall.recall(recall_query)
        assert (
            tuple(
                (item.memory.canonical_id, round(item.score.total, 12))
                for item in after_recall.items
            )
            == before_ranked
        )
        assert maintenance.reconcile(dry_run=True).repairs_queued == 0
    finally:
        services.close()


def test_vector_provider_outage_during_rebuild_preserves_canonical_and_fts(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_id = add_memory(
        adapter,
        harness,
        key="phase6-rebuild-vector-outage",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:rebuild-vector-outage",
        text="synthetic rebuild provider outage fixture",
    )
    services = phase6_services(harness)
    try:
        _drain(services)
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        canonical_before = (record.revision, record.status, record.canonical_fingerprint)
        maintenance = _maintenance(services)
        services.provider_source.fail = True

        rebuild = maintenance.rebuild_owner()
        _drain(services, "rebuild-outage-worker")
        services.phase5.session.expire_all()
        record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
        assert (record.revision, record.status, record.canonical_fingerprint) == canonical_before
        assert rebuild.canonical_checksum_before == rebuild.canonical_checksum_after
        assert services.fts.get_metadata(services.phase5.repository.owner_id, str(memory_id))
        assert (
            services.vector.get_metadata(services.phase5.repository.owner_id, str(memory_id))
            is None
        )
        coverage = maintenance.coverage()
        assert coverage.canonical_active_eligible_count == 1
        assert coverage.fts_current_count == 1
        assert coverage.vector_current_count == 0
        assert coverage.vector_missing_count == 1
        assert coverage.failed_count == 1
        assert coverage.degraded
        assert not coverage.rollout_ready
    finally:
        services.close()


def test_reconciliation_checkpoint_is_bounded_without_false_ghosts(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    for index in range(3):
        add_memory(
            adapter,
            harness,
            key=f"phase6-checkpoint-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:checkpoint_{index}",
            text=f"synthetic checkpoint fixture {index}",
        )
    services = phase6_services(harness)
    try:
        _drain(services)
        maintenance = _maintenance(services)
        first = maintenance.reconcile(dry_run=True, limit=1)
        assert first.checked == 1
        assert first.fts_metadata_checked == 1
        assert first.vector_metadata_checked == 1
        assert first.next_checkpoint is not None
        assert first.next_checkpoint.startswith("v1:")
        assert len(first.next_checkpoint) <= 128
        assert first.ghost_fts == first.ghost_vector == 0
        second = maintenance.reconcile(
            dry_run=True,
            limit=1,
            checkpoint=first.next_checkpoint,
        )
        assert second.checked == 1
        assert second.next_checkpoint is not None
        assert second.next_checkpoint != first.next_checkpoint
        assert second.ghost_fts == second.ghost_vector == 0
        with pytest.raises(ValueError, match="reconciliation_checkpoint_invalid"):
            maintenance.reconcile(dry_run=True, checkpoint="-" * 36)

        privileged = PrivilegedGlobalMemoryV2Maintenance([maintenance], authorized=True)
        global_first = privileged.reconcile_all(dry_run=True, limit=1)[0]
        assert global_first.checked == 1
        assert global_first.next_checkpoint is not None
        global_second = privileged.reconcile_all(
            dry_run=True,
            limit=1,
            checkpoints={global_first.owner_id: global_first.next_checkpoint},
        )[0]
        assert global_second.checked == 1
        assert global_second.checkpoint == global_first.next_checkpoint
        with pytest.raises(ValueError, match="unknown_privileged_maintenance_owner"):
            privileged.reconcile_all(
                dry_run=True,
                checkpoints={uuid4(): None},
            )
    finally:
        services.close()


def test_reconciliation_pages_derived_only_ghosts_and_repairs_persisted_state(
    tmp_path,
) -> None:
    harness, adapter = phase6_harness(tmp_path)
    memory_ids = [
        add_memory(
            adapter,
            harness,
            key=f"phase6-derived-checkpoint-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:derived_checkpoint_{index}",
            text=f"synthetic derived checkpoint fixture {index}",
        )
        for index in range(3)
    ]
    services = phase6_services(harness)
    try:
        _drain(services)
        owner_id = services.phase5.repository.owner_id
        source_record = services.phase5.session.get(MemoryRecordV2, str(memory_ids[0]))
        source_document = services.processor.document_builder.build(
            source_record,
            now=datetime.now(UTC),
        )
        ghost_id = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        ghost_document = source_document.model_copy(update={"memory_id": ghost_id})
        services.fts.upsert(ghost_document)
        services.vector.upsert(
            ghost_document,
            services.provider.embed(ghost_document.display_text),
            services.provider,
        )

        reports = []
        checkpoint = None
        while True:
            report = _maintenance(services).reconcile(
                dry_run=False,
                limit=1,
                checkpoint=checkpoint,
            )
            reports.append(report)
            assert report.checked <= 1
            assert report.fts_metadata_checked <= 1
            assert report.vector_metadata_checked <= 1
            if report.next_checkpoint is None:
                break
            checkpoint = report.next_checkpoint
            assert len(reports) < 10

        assert len(reports) == 4
        assert sum(report.checked for report in reports) == 3
        assert sum(report.fts_metadata_checked for report in reports) == 4
        assert sum(report.vector_metadata_checked for report in reports) == 4
        assert sum(report.ghost_fts for report in reports) == 1
        assert sum(report.ghost_vector for report in reports) == 1
        assert sum(report.repairs_queued for report in reports) == 2

        _drain(services, "derived-checkpoint-repair-worker")
        services.phase5.session.expire_all()
        assert services.phase5.session.get(MemoryRecordV2, str(ghost_id)) is None
        assert services.fts.get_metadata(owner_id, str(ghost_id)) is None
        assert services.vector.get_metadata(owner_id, str(ghost_id)) is None
        for memory_id in memory_ids:
            record = services.phase5.session.get(MemoryRecordV2, str(memory_id))
            assert record is not None
            assert record.status == "active"
            assert services.fts.get_metadata(owner_id, str(memory_id)) is not None
            assert services.vector.get_metadata(owner_id, str(memory_id)) is not None
    finally:
        services.close()


def test_maintenance_uses_configured_batch_retry_and_alert_policy(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    for index in range(2):
        add_memory(
            adapter,
            harness,
            key=f"phase6-settings-{index}",
            memory_type=MemoryType.KNOWLEDGE,
            domain="learning",
            slot=f"knowledge:learning:settings_{index}",
            text=f"synthetic settings fixture {index}",
        )
    services = phase6_services(harness)
    try:
        settings = Settings(
            memory_v2_reconciliation_batch_size=1,
            memory_v2_retry_max_attempts=9,
            memory_v2_alert_oldest_pending_seconds=17,
            memory_v2_alert_dead_letter_count=2,
            memory_v2_alert_min_coverage_ratio=0.8,
            memory_v2_alert_consecutive_provider_failures=4,
            memory_v2_alert_stale_ghost_rate=0.2,
            memory_v2_alert_lease_expiration_rate=0.3,
        )
        maintenance = MemoryV2IndexMaintenance.from_settings(
            services.phase5.session.get_bind(),
            settings=settings,
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            fts_index=services.fts,
            vector_index=services.vector,
            repair_scheduler=services.processor.schedule_repair,
            embedding_provider=services.provider,
            provider_health=services.provider.health,
            metric_reader=services.metrics.snapshot,
        )
        report = maintenance.reconcile(dry_run=True)
        assert report.checked == 1
        assert report.next_checkpoint is not None
        assert maintenance.max_attempts == 9
        assert maintenance.alert_oldest_pending_seconds == 17
        assert maintenance.alert_dead_letter_count == 2
        assert maintenance.alert_min_coverage_ratio == 0.8
        assert maintenance.alert_consecutive_provider_failures == 4
        assert maintenance.alert_stale_ghost_rate == 0.2
        assert maintenance.alert_lease_expiration_rate == 0.3
    finally:
        services.close()


def test_global_rebuild_requires_privilege_and_health_contains_no_plaintext(tmp_path) -> None:
    harness, adapter = phase6_harness(tmp_path)
    secret_fixture = "phase6-plaintext-must-not-appear"
    add_memory(
        adapter,
        harness,
        key="phase6-health-redaction",
        memory_type=MemoryType.KNOWLEDGE,
        domain="learning",
        slot="knowledge:learning:health",
        text=secret_fixture,
    )
    services = phase6_services(harness)
    try:
        _drain(services)
        maintenance = _maintenance(services)
        services.metrics.record(
            {
                DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT: 2,
                DerivedMetricCode.SEMANTIC_STALE_HIT_DROP: 3,
            }
        )
        with pytest.raises(PermissionError, match="privileged_memory_v2"):
            PrivilegedGlobalMemoryV2Maintenance([maintenance], authorized=False)
        privileged = PrivilegedGlobalMemoryV2Maintenance([maintenance], authorized=True)
        assert len(privileged.rebuild_all()) == 1
        assert len(privileged.reconcile_all(dry_run=True)) == 1
        aggregate = privileged.coverage()
        assert aggregate.owner_count == 1
        assert aggregate.canonical_active_eligible_count == 1
        assert aggregate.fts_missing_count == 1
        assert aggregate.vector_missing_count == 1
        assert aggregate.pending_outbox_count == 1
        structured_unhealthy = MemoryV2IndexMaintenance(
            services.phase5.session.get_bind(),
            owner_id=services.phase5.repository.owner_id,
            database_identity=services.phase5.repository.database_identity,
            fts_index=services.fts,
            vector_index=services.vector,
            repair_scheduler=services.processor.schedule_repair,
            embedding_provider=services.provider,
            provider_health=lambda: ProviderHealth(
                provider="structured-health-fixture",
                model="structured-health-model",
                provider_version="1",
                healthy=False,
                failure_code="embedding_unavailable",
            ),
            metric_reader=services.metrics.snapshot,
        )
        assert not structured_unhealthy.coverage().provider_healthy
        assert aggregate.wrong_owner_hit_count == 2
        assert aggregate.stale_hit_drop_count == 3
        assert aggregate.embedding_model_coverage_count == 0
        assert aggregate.degraded_owner_count == 1
        assert not aggregate.rollout_ready
        _drain(services, "global-rebuild-worker")
        report = maintenance.coverage().model_dump_json()
        assert secret_fixture not in report
        assert '"rollout_ready":true' in report
        assert '"wrong_owner_hit_count":2' in report
        assert '"stale_hit_drop_count":3' in report
    finally:
        services.close()
