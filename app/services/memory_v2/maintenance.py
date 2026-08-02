"""Owner-scoped reconciliation, rebuild, and bounded Phase 6 health reports."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.memory_v2 import (
    MemoryDerivedStateV2,
    MemoryOutboxDeliveryV2,
    MemoryOutboxV2,
)
from app.repositories.memory_v2 import MemoryV2Repository
from app.services.memory_v2.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory_v2.indexes import DerivedDocumentBuilder
from app.services.memory_v2.phase6_contracts import (
    CoverageReport,
    DerivedMetricCode,
    DerivedTarget,
    DerivedTargetState,
    GlobalCoverageReport,
    IndexRepairRequest,
    RebuildResult,
    RebuildVerification,
    ReconciliationReport,
)
from app.services.memory_v2.versions import (
    DERIVED_DOCUMENT_VERSION,
    EMBEDDING_DOCUMENT_VERSION,
    EMBEDDING_IDENTITY_VERSION,
    VECTOR_METADATA_VERSION,
)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


_RECONCILIATION_CHECKPOINT_VERSION = "v1"
_RECONCILIATION_CURSOR_START = "-"
_RECONCILIATION_CURSOR_DONE = "!"


def _parse_reconciliation_checkpoint(
    checkpoint: str | None,
) -> tuple[tuple[str | None, str | None, str | None], str | None]:
    if checkpoint is None:
        return (None, None, None), None
    if len(checkpoint) > 128:
        raise ValueError("reconciliation_checkpoint_invalid")
    parts = checkpoint.split(":")
    if len(parts) == 1:
        try:
            canonical = str(UUID(checkpoint))
        except ValueError as exc:
            raise ValueError("reconciliation_checkpoint_invalid") from exc
        cursors = (canonical, None, None)
        return cursors, _format_reconciliation_checkpoint(cursors)
    if len(parts) != 4 or parts[0] != _RECONCILIATION_CHECKPOINT_VERSION:
        raise ValueError("reconciliation_checkpoint_invalid")

    def parse_cursor(value: str) -> str | None:
        if value == _RECONCILIATION_CURSOR_START:
            return None
        if value == _RECONCILIATION_CURSOR_DONE:
            return value
        try:
            return str(UUID(value))
        except ValueError as exc:
            raise ValueError("reconciliation_checkpoint_invalid") from exc

    cursors = tuple(parse_cursor(value) for value in parts[1:])
    return cursors, _format_reconciliation_checkpoint(cursors)


def _format_reconciliation_checkpoint(
    cursors: tuple[str | None, str | None, str | None],
) -> str:
    return ":".join(
        (
            _RECONCILIATION_CHECKPOINT_VERSION,
            *(_RECONCILIATION_CURSOR_START if cursor is None else cursor for cursor in cursors),
        )
    )


def _next_cursor(items: list, *, limit: int, identifier) -> tuple[list, str]:
    page = items[:limit]
    if len(items) > limit and page:
        return page, str(UUID(str(identifier(page[-1]))))
    return page, _RECONCILIATION_CURSOR_DONE


class MemoryV2IndexMaintenance:
    def __init__(
        self,
        engine: Engine,
        *,
        owner_id: str,
        database_identity: str,
        fts_index,
        vector_index,
        repair_scheduler,
        embedding_provider=None,
        provider_health=lambda: True,
        metric_reader=lambda: {},
        max_attempts: int = 5,
        reconciliation_batch_size: int = 250,
        alert_oldest_pending_seconds: int = 900,
        alert_dead_letter_count: int = 1,
        alert_min_coverage_ratio: float = 0.95,
        alert_consecutive_provider_failures: int = 3,
        alert_stale_ghost_rate: float = 0.05,
        alert_lease_expiration_rate: float = 0.1,
    ) -> None:
        self.engine = engine
        self.owner_id = str(UUID(owner_id))
        self.database_identity = database_identity
        self.fts_index = fts_index
        self.vector_index = vector_index
        self.repair_scheduler = repair_scheduler
        self.embedding_provider = embedding_provider
        self.provider_health = provider_health
        self.metric_reader = metric_reader
        self.max_attempts = max_attempts
        self.reconciliation_batch_size = min(max(1, reconciliation_batch_size), 5_000)
        self.alert_oldest_pending_seconds = alert_oldest_pending_seconds
        self.alert_dead_letter_count = alert_dead_letter_count
        self.alert_min_coverage_ratio = alert_min_coverage_ratio
        self.alert_consecutive_provider_failures = alert_consecutive_provider_failures
        self.alert_stale_ghost_rate = alert_stale_ghost_rate
        self.alert_lease_expiration_rate = alert_lease_expiration_rate
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.builder = DerivedDocumentBuilder()

    @classmethod
    def from_settings(cls, engine: Engine, *, settings: Settings, **dependencies):
        """Construct maintenance with the configured retry, batch, and alert policy."""
        return cls(
            engine,
            max_attempts=settings.memory_v2_retry_max_attempts,
            reconciliation_batch_size=settings.memory_v2_reconciliation_batch_size,
            alert_oldest_pending_seconds=settings.memory_v2_alert_oldest_pending_seconds,
            alert_dead_letter_count=settings.memory_v2_alert_dead_letter_count,
            alert_min_coverage_ratio=settings.memory_v2_alert_min_coverage_ratio,
            alert_consecutive_provider_failures=(
                settings.memory_v2_alert_consecutive_provider_failures
            ),
            alert_stale_ghost_rate=settings.memory_v2_alert_stale_ghost_rate,
            alert_lease_expiration_rate=settings.memory_v2_alert_lease_expiration_rate,
            **dependencies,
        )

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        limit: int | None = None,
        checkpoint: str | None = None,
    ) -> ReconciliationReport:
        current = now or datetime.now(UTC)
        cursors, normalized_checkpoint = _parse_reconciliation_checkpoint(checkpoint)
        canonical_cursor, fts_cursor, vector_cursor = cursors
        requested_limit = self.reconciliation_batch_size if limit is None else limit
        bounded_limit = min(max(1, requested_limit), 1_000)
        with self._sessions() as session:
            repository = MemoryV2Repository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            if canonical_cursor == _RECONCILIATION_CURSOR_DONE:
                records = []
                next_canonical_cursor = _RECONCILIATION_CURSOR_DONE
            else:
                canonical_page = repository.list_index_candidates(
                    now=current,
                    after_memory_id=canonical_cursor,
                    limit=min(1_001, bounded_limit + 1),
                )
                records, next_canonical_cursor = _next_cursor(
                    canonical_page,
                    limit=bounded_limit,
                    identifier=lambda item: item.id,
                )
            if fts_cursor == _RECONCILIATION_CURSOR_DONE:
                fts_rows = []
                next_fts_cursor = _RECONCILIATION_CURSOR_DONE
            else:
                fts_page = self.fts_index.list_metadata_for_owner(
                    self.owner_id,
                    after_memory_id=fts_cursor,
                    limit=min(1_001, bounded_limit + 1),
                )
                fts_rows, next_fts_cursor = _next_cursor(
                    [item for item in fts_page if item is not None],
                    limit=bounded_limit,
                    identifier=lambda item: item["memory_id"],
                )
            if vector_cursor == _RECONCILIATION_CURSOR_DONE:
                vector_rows = []
                next_vector_cursor = _RECONCILIATION_CURSOR_DONE
            else:
                vector_page = self.vector_index.list_metadata_for_owner(
                    self.owner_id,
                    after_memory_id=vector_cursor,
                    limit=min(1_001, bounded_limit + 1),
                )
                vector_rows, next_vector_cursor = _next_cursor(
                    [item for item in vector_page if item is not None],
                    limit=bounded_limit,
                    identifier=lambda item: item["memory_id"],
                )
            metadata_ids = {
                str(item["memory_id"])
                for item in (*fts_rows, *vector_rows)
                if item.get("memory_id") is not None
            }
            canonical_by_metadata_id = {
                memory_id: repository.get_owner_record_any_lifecycle(memory_id)
                for memory_id in metadata_ids
            }
        documents = {
            str(document.memory_id): document
            for record in records
            if (document := self.builder.build(record, now=current)) is not None
        }
        fts_for_documents = {
            memory_id: metadata
            for memory_id in documents
            if (metadata := self.fts_index.get_metadata(self.owner_id, memory_id)) is not None
        }
        vectors_for_documents = {
            memory_id: metadata
            for memory_id in documents
            if (metadata := self.vector_index.get_metadata(self.owner_id, memory_id)) is not None
        }
        repairs: list[tuple[str, str, DerivedTarget, str, str | None]] = []
        owner_mismatch_rows_by_key = {
            (str(item["memory_id"]), target): (item, target)
            for rows, target in (
                ((*fts_rows, *fts_for_documents.values()), DerivedTarget.FTS),
                ((*vector_rows, *vectors_for_documents.values()), DerivedTarget.VECTOR),
            )
            for item in rows
            if str(item.get("owner_id")) != self.owner_id
        }
        owner_mismatch_rows = list(owner_mismatch_rows_by_key.values())
        for item, target in owner_mismatch_rows:
            repairs.append(
                (
                    str(item["memory_id"]),
                    "delete",
                    target,
                    "owner_metadata_mismatch",
                    str(item.get("content_hash") or "") or None,
                )
            )
        fts_scan = {
            str(item["memory_id"]): item
            for item in fts_rows
            if str(item.get("owner_id")) == self.owner_id
        }
        vector_scan = {
            str(item["memory_id"]): item
            for item in vector_rows
            if str(item.get("owner_id")) == self.owner_id
        }
        fts = {
            memory_id: item
            for memory_id, item in fts_for_documents.items()
            if str(item.get("owner_id")) == self.owner_id
        }
        vectors = {
            memory_id: item
            for memory_id, item in vectors_for_documents.items()
            if str(item.get("owner_id")) == self.owner_id
        }
        missing_fts = stale_fts = missing_vector = stale_vector = wrong_model = 0
        for memory_id, document in documents.items():
            if memory_id not in fts:
                missing_fts += 1
                repairs.append((memory_id, "upsert", DerivedTarget.FTS, "missing_fts", None))
            elif not self._fts_metadata_current(fts[memory_id], document):
                stale_fts += 1
                repairs.append(
                    (
                        memory_id,
                        "upsert",
                        DerivedTarget.FTS,
                        "stale_fts",
                        str(fts[memory_id]["content_hash"]),
                    )
                )
            if memory_id not in vectors:
                missing_vector += 1
                repairs.append((memory_id, "upsert", DerivedTarget.VECTOR, "missing_vector", None))
            elif vectors[memory_id]["content_hash"] != document.content_hash:
                stale_vector += 1
                repairs.append(
                    (
                        memory_id,
                        "upsert",
                        DerivedTarget.VECTOR,
                        "stale_vector",
                        str(vectors[memory_id]["content_hash"]),
                    )
                )
            elif not self._vector_metadata_current(vectors[memory_id], document):
                wrong_model += 1
                repairs.append(
                    (
                        memory_id,
                        "upsert",
                        DerivedTarget.VECTOR,
                        "wrong_embedding_identity",
                        str(vectors[memory_id]["content_hash"]),
                    )
                )

        ghost_by_target: dict[DerivedTarget, list[str]] = {
            DerivedTarget.FTS: [],
            DerivedTarget.VECTOR: [],
        }
        inactive_indexed = expired_indexed = policy_ineligible_indexed = 0
        for metadata, target in (
            (fts_scan, DerivedTarget.FTS),
            (vector_scan, DerivedTarget.VECTOR),
        ):
            for memory_id, item in metadata.items():
                record = canonical_by_metadata_id[memory_id]
                reason = None
                if record is None:
                    ghost_by_target[target].append(memory_id)
                    reason = f"ghost_{target.value}"
                elif record.status != MemoryLifecycleState.ACTIVE.value:
                    inactive_indexed += 1
                    reason = f"inactive_{target.value}"
                elif record.expires_at is not None and _aware(record.expires_at) <= _aware(current):
                    expired_indexed += 1
                    reason = f"expired_{target.value}"
                elif (
                    record.sensitivity != Sensitivity.NORMAL.value
                    or self.builder.build(record, now=current) is None
                ):
                    policy_ineligible_indexed += 1
                    reason = f"policy_ineligible_{target.value}"
                if reason is not None:
                    repairs.append(
                        (
                            memory_id,
                            "delete",
                            target,
                            reason,
                            str(item["content_hash"]),
                        )
                    )
        checked_ids = set(documents)
        derived = []
        deliveries = []
        delivery_events = []
        if checked_ids:
            with self._sessions() as session:
                derived = list(
                    session.scalars(
                        select(MemoryDerivedStateV2).where(
                            MemoryDerivedStateV2.owner_id == self.owner_id,
                            MemoryDerivedStateV2.memory_id.in_(checked_ids),
                        )
                    )
                )
                delivery_events = list(
                    session.scalars(
                        select(MemoryOutboxV2).where(
                            MemoryOutboxV2.owner_id == self.owner_id,
                            MemoryOutboxV2.memory_id.in_(checked_ids),
                        )
                    )
                )
                event_ids = {item.id for item in delivery_events}
                if event_ids:
                    deliveries = list(
                        session.scalars(
                            select(MemoryOutboxDeliveryV2).where(
                                MemoryOutboxDeliveryV2.owner_id == self.owner_id,
                                MemoryOutboxDeliveryV2.event_id.in_(event_ids),
                            )
                        )
                    )
        done_upserts = [
            event
            for event in delivery_events
            if event.event_kind == "canonical_upsert" and event.state == "done"
        ]
        state_by_key = {(item.memory_id, item.target): item for item in derived}
        event_by_id = {item.id: item for item in delivery_events}
        metadata_by_target = {
            DerivedTarget.FTS.value: fts,
            DerivedTarget.VECTOR.value: vectors,
        }
        pending_already_current = sum(
            item.state
            in {
                DerivedTargetState.PENDING.value,
                DerivedTargetState.FAILED.value,
                DerivedTargetState.PROCESSING.value,
            }
            and (event := event_by_id.get(item.event_id)) is not None
            and event.memory_id in checked_ids
            and (current_state := state_by_key.get((event.memory_id, item.target))) is not None
            and current_state.state == DerivedTargetState.CURRENT.value
            and current_state.canonical_revision == event.canonical_revision
            and (metadata := metadata_by_target[item.target].get(event.memory_id)) is not None
            and (
                self._fts_metadata_current(metadata, documents[event.memory_id])
                if item.target == DerivedTarget.FTS.value
                else self._vector_metadata_current(metadata, documents[event.memory_id])
            )
            for item in deliveries
        )
        deliveries_by_event: dict[str, set[str]] = {}
        for delivery in deliveries:
            deliveries_by_event.setdefault(delivery.event_id, set()).add(delivery.target)

        def done_event_missing(event) -> bool:
            if event.memory_id not in checked_ids:
                return False
            document = documents[event.memory_id]
            if (
                event.canonical_revision != document.canonical_revision
                or event.content_hash != document.canonical_content_hash
            ):
                return False
            targets = deliveries_by_event.get(event.id)
            if not targets:
                requested = (event.event_payload_json or {}).get("target")
                targets = (
                    {requested} if requested in metadata_by_target else set(metadata_by_target)
                )
            for target in targets:
                metadata = metadata_by_target[target].get(event.memory_id)
                if metadata is None:
                    return True
                if target == DerivedTarget.FTS.value:
                    if not self._fts_metadata_current(metadata, document):
                        return True
                elif not self._vector_metadata_current(metadata, document):
                    return True
            return False

        done_missing = sum(done_event_missing(event) for event in done_upserts)
        next_cursors = (
            next_canonical_cursor,
            next_fts_cursor,
            next_vector_cursor,
        )
        next_checkpoint = (
            None
            if all(cursor == _RECONCILIATION_CURSOR_DONE for cursor in next_cursors)
            else _format_reconciliation_checkpoint(next_cursors)
        )
        if not dry_run:
            seen = set()
            for memory_id, action, target, reason, expected in repairs:
                key = (memory_id, action, target)
                if key in seen:
                    continue
                seen.add(key)
                self.repair_scheduler(
                    IndexRepairRequest(
                        owner_id=UUID(self.owner_id),
                        memory_id=UUID(memory_id),
                        action=action,
                        target=target,
                        reason=reason,
                        expected_hash=expected,
                    )
                )
        return ReconciliationReport(
            owner_id=UUID(self.owner_id),
            checked=len(records),
            fts_metadata_checked=len(fts_rows),
            vector_metadata_checked=len(vector_rows),
            missing_fts=missing_fts,
            missing_vector=missing_vector,
            stale_fts=stale_fts,
            stale_vector=stale_vector,
            ghost_fts=len(ghost_by_target[DerivedTarget.FTS]),
            ghost_vector=len(ghost_by_target[DerivedTarget.VECTOR]),
            inactive_indexed=inactive_indexed,
            expired_indexed=expired_indexed,
            policy_ineligible_indexed=policy_ineligible_indexed,
            wrong_model_vector=wrong_model,
            owner_metadata_mismatch=len(owner_mismatch_rows),
            pending_already_current=pending_already_current,
            done_missing_derived=done_missing,
            repairs_queued=(
                0 if dry_run else len({(item[0], item[1], item[2]) for item in repairs})
            ),
            dry_run=dry_run,
            checkpoint=normalized_checkpoint,
            next_checkpoint=next_checkpoint,
        )

    def rebuild_owner(self, *, now: datetime | None = None) -> RebuildResult:
        current = now or datetime.now(UTC)
        before = self._canonical_checksum(current)
        documents = self._canonical_documents(current)
        expected_checksum = self._metadata_checksum(
            {
                memory_id: {"content_hash": document.content_hash}
                for memory_id, document in documents.items()
            }
        )
        fts_cleared = self.fts_index.clear_owner(self.owner_id)
        vector_cleared = self.vector_index.clear_owner(self.owner_id)
        with self._sessions.begin() as session:
            session.execute(
                update(MemoryDerivedStateV2)
                .where(MemoryDerivedStateV2.owner_id == self.owner_id)
                .values(
                    state=DerivedTargetState.DELETED.value,
                    last_error_code=None,
                    updated_at=current,
                )
            )
            if documents:
                session.execute(
                    update(MemoryDerivedStateV2)
                    .where(
                        MemoryDerivedStateV2.owner_id == self.owner_id,
                        MemoryDerivedStateV2.memory_id.in_(tuple(documents)),
                    )
                    .values(
                        state=DerivedTargetState.PENDING.value,
                        last_error_code=None,
                        updated_at=current,
                    )
                )
        for memory_id in documents:
            self.repair_scheduler(
                IndexRepairRequest(
                    owner_id=UUID(self.owner_id),
                    memory_id=UUID(memory_id),
                    action="upsert",
                    target=None,
                    reason="owner_rebuild",
                )
            )
        after = self._canonical_checksum(current)
        return RebuildResult(
            owner_id=UUID(self.owner_id),
            canonical_checksum_before=before,
            canonical_checksum_after=after,
            queued=len(documents),
            canonical_eligible_count=len(documents),
            fts_cleared_count=fts_cleared,
            vector_cleared_count=vector_cleared,
            pending_target_count=len(documents) * len(DerivedTarget),
            expected_derived_checksum=expected_checksum,
        )

    def verify_owner_rebuild(
        self,
        result: RebuildResult,
        *,
        now: datetime | None = None,
    ) -> RebuildVerification:
        if result.owner_id != UUID(self.owner_id):
            raise ValueError("rebuild_owner_mismatch")
        current = now or datetime.now(UTC)
        documents = self._canonical_documents(current)
        expected = {
            memory_id: {"content_hash": document.content_hash}
            for memory_id, document in documents.items()
        }
        fts = {
            str(item["memory_id"]): item
            for item in self.fts_index.list_metadata_for_owner(self.owner_id)
            if str(item.get("owner_id")) == self.owner_id
        }
        vectors = {
            str(item["memory_id"]): item
            for item in self.vector_index.list_metadata_for_owner(self.owner_id)
            if item is not None and str(item.get("owner_id")) == self.owner_id
        }
        fts_bad = sum(
            memory_id not in fts or not self._fts_metadata_current(fts[memory_id], document)
            for memory_id, document in documents.items()
        ) + len(set(fts) - set(documents))
        vector_bad = sum(
            memory_id not in vectors
            or not self._vector_metadata_current(vectors[memory_id], document)
            for memory_id, document in documents.items()
        ) + len(set(vectors) - set(documents))
        expected_checksum = self._metadata_checksum(expected)
        fts_checksum = self._metadata_checksum(fts)
        vector_checksum = self._metadata_checksum(vectors)
        canonical_checksum = self._canonical_checksum(current)
        equivalent = bool(
            canonical_checksum == result.canonical_checksum_before
            and expected_checksum == result.expected_derived_checksum
            and not fts_bad
            and not vector_bad
            and fts_checksum == expected_checksum
            and vector_checksum == expected_checksum
        )
        return RebuildVerification(
            owner_id=UUID(self.owner_id),
            canonical_checksum=canonical_checksum,
            expected_derived_checksum=expected_checksum,
            fts_checksum=fts_checksum,
            vector_checksum=vector_checksum,
            canonical_eligible_count=len(documents),
            fts_count=len(fts),
            vector_count=len(vectors),
            fts_missing_or_stale=fts_bad,
            vector_missing_or_stale=vector_bad,
            equivalent=equivalent,
        )

    def coverage(self, *, now: datetime | None = None) -> CoverageReport:
        current = now or datetime.now(UTC)
        documents = self._canonical_documents(current)
        try:
            fts_metadata = [
                item
                for item in self.fts_index.list_metadata_for_owner(self.owner_id)
                if item is not None and str(item.get("owner_id")) == self.owner_id
            ]
        except Exception:
            fts_metadata = []
        try:
            vector_metadata = [
                item
                for item in self.vector_index.list_metadata_for_owner(self.owner_id)
                if item is not None and str(item.get("owner_id")) == self.owner_id
            ]
        except Exception:
            vector_metadata = []
        with self._sessions() as session:
            repository = MemoryV2Repository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            states = list(
                session.scalars(
                    select(MemoryDerivedStateV2).where(
                        MemoryDerivedStateV2.owner_id == self.owner_id
                    )
                )
            )
            outbox = list(
                session.scalars(
                    select(MemoryOutboxV2).where(MemoryOutboxV2.owner_id == self.owner_id)
                )
            )
            deliveries = list(
                session.scalars(
                    select(MemoryOutboxDeliveryV2).where(
                        MemoryOutboxDeliveryV2.owner_id == self.owner_id
                    )
                )
            )
            indexed_ids = {str(item["memory_id"]) for item in (*fts_metadata, *vector_metadata)}
            existing_canonical_ids = {
                memory_id
                for memory_id in indexed_ids
                if repository.get_owner_record_any_lifecycle(memory_id) is not None
            }
        count = len(documents)
        eligible_ids = set(documents)
        by_target = {
            target: [item for item in states if item.target == target.value]
            for target in DerivedTarget
        }
        fts_by_id = {str(item["memory_id"]): item for item in fts_metadata}
        vector_by_id = {str(item["memory_id"]): item for item in vector_metadata}
        fts_missing = sum(memory_id not in fts_by_id for memory_id in eligible_ids)
        fts_stale = sum(
            memory_id in fts_by_id
            and not self._fts_metadata_current(fts_by_id[memory_id], document)
            for memory_id, document in documents.items()
        )
        fts_current = count - fts_missing - fts_stale

        vector_missing = sum(memory_id not in vector_by_id for memory_id in eligible_ids)
        vector_stale = sum(
            memory_id in vector_by_id
            and not self._vector_metadata_current(vector_by_id[memory_id], document)
            for memory_id, document in documents.items()
        )
        vector_current = count - vector_missing - vector_stale
        vector_na = sum(
            item.state == DerivedTargetState.NOT_APPLICABLE.value
            for item in by_target[DerivedTarget.VECTOR]
        )
        pending_dates = [item.created_at for item in outbox if item.state in {"pending", "failed"}]
        oldest = 0
        if pending_dates:
            earliest = min(
                item.replace(tzinfo=UTC) if item.tzinfo is None else item for item in pending_dates
            )
            oldest = max(0, int((current - earliest).total_seconds()))
        dead = sum(item.state == DerivedTargetState.DEAD_LETTER.value for item in deliveries)
        try:
            health_result = self.provider_health()
            if isinstance(health_result, bool):
                healthy = health_result
            else:
                structured_health = getattr(health_result, "healthy", None)
                healthy = structured_health if isinstance(structured_health, bool) else False
        except Exception:
            healthy = False
        try:
            fts_healthy = bool(self.fts_index.health().healthy)
        except Exception:
            fts_healthy = False
        try:
            vector_healthy = bool(self.vector_index.health().healthy)
        except Exception:
            vector_healthy = False
        denominator = max(1, count)
        coverage_ratio = 1.0 if count == 0 else min(fts_current, vector_current) / denominator
        ghost_count = sum(
            str(item["memory_id"]) not in existing_canonical_ids
            for item in (*fts_metadata, *vector_metadata)
        )
        stale_ghost_rate = min(
            1.0,
            (fts_stale + vector_stale + ghost_count) / denominator,
        )
        expired_leases = sum(
            item.state == DerivedTargetState.PROCESSING.value
            and item.lease_expires_at is not None
            and _aware(item.lease_expires_at) <= _aware(current)
            for item in deliveries
        )
        lease_expiration_rate = min(1.0, expired_leases / max(1, len(deliveries)))
        consecutive_failures = int(getattr(self.embedding_provider, "consecutive_failures", 0))
        try:
            metrics = self.metric_reader()
        except Exception:
            metrics = {}
        alerts = []
        if oldest >= self.alert_oldest_pending_seconds:
            alerts.append("oldest_pending_age")
        if dead >= self.alert_dead_letter_count and self.alert_dead_letter_count > 0:
            alerts.append("dead_letter_count")
        if coverage_ratio < self.alert_min_coverage_ratio:
            alerts.append("coverage_ratio")
        if consecutive_failures >= self.alert_consecutive_provider_failures:
            alerts.append("consecutive_provider_failures")
        if stale_ghost_rate >= self.alert_stale_ghost_rate and (
            fts_stale or vector_stale or ghost_count
        ):
            alerts.append("stale_ghost_rate")
        if lease_expiration_rate >= self.alert_lease_expiration_rate and expired_leases:
            alerts.append("lease_expiration_rate")
        pending_outbox = sum(item.state == "pending" for item in outbox)
        processing = sum(item.state == "processing" for item in deliveries)
        failed = sum(item.state == DerivedTargetState.FAILED.value for item in deliveries)
        unresolved = bool(
            fts_missing
            or fts_stale
            or vector_missing
            or vector_stale
            or pending_outbox
            or processing
            or failed
            or dead
        )
        degraded = bool(
            unresolved or alerts or not healthy or not fts_healthy or not vector_healthy
        )
        return CoverageReport(
            owner_id=UUID(self.owner_id),
            canonical_active_eligible_count=count,
            fts_current_count=fts_current,
            fts_missing_count=fts_missing,
            fts_stale_count=fts_stale,
            vector_current_count=vector_current,
            vector_missing_count=vector_missing,
            vector_stale_count=vector_stale,
            vector_not_applicable_count=vector_na,
            pending_outbox_count=pending_outbox,
            processing_count=processing,
            failed_count=failed,
            dead_letter_count=dead,
            oldest_pending_age_seconds=oldest,
            maximum_attempts=max((item.attempts for item in deliveries), default=0),
            lease_expired_count=expired_leases,
            ghost_count=ghost_count,
            wrong_owner_hit_count=int(metrics.get(DerivedMetricCode.SEMANTIC_WRONG_OWNER_HIT, 0)),
            stale_hit_drop_count=int(metrics.get(DerivedMetricCode.SEMANTIC_STALE_HIT_DROP, 0)),
            provider_healthy=healthy,
            fts_healthy=fts_healthy,
            vector_index_healthy=vector_healthy,
            embedding_provider=getattr(self.embedding_provider, "provider_name", None),
            embedding_model=getattr(self.embedding_provider, "model_name", None),
            embedding_provider_version=getattr(self.embedding_provider, "provider_version", None),
            embedding_model_coverage_count=sum(
                self._vector_metadata_current(vector_by_id[memory_id], document)
                for memory_id, document in documents.items()
                if memory_id in vector_by_id
            ),
            consecutive_provider_failures=consecutive_failures,
            stale_ghost_rate=stale_ghost_rate,
            lease_expiration_rate=lease_expiration_rate,
            degraded=degraded,
            rollout_ready=not degraded,
            alert_codes=tuple(alerts),
        )

    def _canonical_checksum(self, now: datetime) -> str:
        records = self._canonical_documents(now)
        material = "\n".join(
            (f"{memory_id}:{item.canonical_revision}:active:{item.canonical_content_hash}")
            for memory_id, item in sorted(records.items())
        )
        return hashlib.sha256(material.encode()).hexdigest()

    def _canonical_documents(self, now: datetime):
        documents = {}
        checkpoint = None
        with self._sessions() as session:
            repository = MemoryV2Repository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            while True:
                page = repository.list_index_candidates(
                    now=now,
                    after_memory_id=checkpoint,
                    limit=1_000,
                )
                for record in page:
                    document = self.builder.build(record, now=now)
                    if document is not None:
                        documents[record.id] = document
                if len(page) < 1_000:
                    break
                checkpoint = page[-1].id
        return documents

    @staticmethod
    def _metadata_checksum(metadata) -> str:
        material = "\n".join(
            f"{memory_id}:{item['content_hash']}" for memory_id, item in sorted(metadata.items())
        )
        return hashlib.sha256(material.encode()).hexdigest()

    @staticmethod
    def _fts_metadata_current(metadata, document) -> bool:
        return bool(
            metadata.get("content_hash") == document.content_hash
            and metadata.get("derived_schema_version") == DERIVED_DOCUMENT_VERSION
        )

    def _vector_metadata_current(self, metadata, document) -> bool:
        embedding = self.builder.build_embedding(document)
        expected_identity = self.embedding_provider is None or (
            metadata.get("provider") == self.embedding_provider.provider_name
            and metadata.get("model") == self.embedding_provider.model_name
            and metadata.get("provider_version") == self.embedding_provider.provider_version
            and metadata.get("dimension") == self.embedding_provider.dimension
        )
        return bool(
            metadata.get("content_hash") == document.content_hash
            and metadata.get("metadata_version") == VECTOR_METADATA_VERSION
            and metadata.get("derived_schema_version") == DERIVED_DOCUMENT_VERSION
            and metadata.get("embedding_document_version") == EMBEDDING_DOCUMENT_VERSION
            and metadata.get("embedding_content_hash") == embedding.content_hash
            and metadata.get("embedding_identity_version") == EMBEDDING_IDENTITY_VERSION
            and expected_identity
        )


class PrivilegedGlobalMemoryV2Maintenance:
    """Global rebuild requires an explicit administrative capability at construction."""

    def __init__(self, maintainers: list[MemoryV2IndexMaintenance], *, authorized: bool) -> None:
        if not authorized:
            raise PermissionError("privileged_memory_v2_maintenance_required")
        self.maintainers = tuple(maintainers)

    def rebuild_all(self) -> tuple[RebuildResult, ...]:
        return tuple(item.rebuild_owner() for item in self.maintainers)

    def reconcile_all(
        self,
        *,
        dry_run: bool = False,
        limit: int | None = None,
        checkpoints: Mapping[str | UUID, str | None] | None = None,
    ) -> tuple[ReconciliationReport, ...]:
        normalized_checkpoints = {
            str(UUID(str(owner_id))): checkpoint
            for owner_id, checkpoint in (checkpoints or {}).items()
        }
        known_owners = {item.owner_id for item in self.maintainers}
        if unknown := set(normalized_checkpoints) - known_owners:
            raise ValueError(f"unknown_privileged_maintenance_owner:{sorted(unknown)[0]}")
        return tuple(
            item.reconcile(
                dry_run=dry_run,
                limit=limit,
                checkpoint=normalized_checkpoints.get(item.owner_id),
            )
            for item in self.maintainers
        )

    def coverage(self) -> GlobalCoverageReport:
        reports = tuple(item.coverage() for item in self.maintainers)
        canonical_count = sum(item.canonical_active_eligible_count for item in reports)
        stale_or_ghost = sum(
            item.fts_stale_count + item.vector_stale_count + item.ghost_count for item in reports
        )
        return GlobalCoverageReport(
            owner_count=len(reports),
            canonical_active_eligible_count=canonical_count,
            fts_current_count=sum(item.fts_current_count for item in reports),
            fts_missing_count=sum(item.fts_missing_count for item in reports),
            fts_stale_count=sum(item.fts_stale_count for item in reports),
            vector_current_count=sum(item.vector_current_count for item in reports),
            vector_missing_count=sum(item.vector_missing_count for item in reports),
            vector_stale_count=sum(item.vector_stale_count for item in reports),
            vector_not_applicable_count=sum(item.vector_not_applicable_count for item in reports),
            pending_outbox_count=sum(item.pending_outbox_count for item in reports),
            processing_count=sum(item.processing_count for item in reports),
            failed_count=sum(item.failed_count for item in reports),
            dead_letter_count=sum(item.dead_letter_count for item in reports),
            oldest_pending_age_seconds=max(
                (item.oldest_pending_age_seconds for item in reports),
                default=0,
            ),
            maximum_attempts=max((item.maximum_attempts for item in reports), default=0),
            lease_expired_count=sum(item.lease_expired_count for item in reports),
            ghost_count=sum(item.ghost_count for item in reports),
            wrong_owner_hit_count=sum(item.wrong_owner_hit_count for item in reports),
            stale_hit_drop_count=sum(item.stale_hit_drop_count for item in reports),
            embedding_model_coverage_count=sum(
                item.embedding_model_coverage_count for item in reports
            ),
            unhealthy_provider_owner_count=sum(not item.provider_healthy for item in reports),
            unhealthy_fts_owner_count=sum(not item.fts_healthy for item in reports),
            unhealthy_vector_owner_count=sum(not item.vector_index_healthy for item in reports),
            maximum_consecutive_provider_failures=max(
                (item.consecutive_provider_failures for item in reports),
                default=0,
            ),
            stale_ghost_rate=min(1.0, stale_or_ghost / max(1, canonical_count)),
            lease_expiration_rate=max(
                (item.lease_expiration_rate for item in reports),
                default=0,
            ),
            degraded_owner_count=sum(item.degraded for item in reports),
            rollout_ready=all(item.rollout_ready for item in reports),
            alert_codes=tuple(sorted({code for item in reports for code in item.alert_codes})),
        )
