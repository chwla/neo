"""Post-commit leasing and independent derived-target processing for memory memory."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.models.memory import (
    MemoryDerivedState,
    MemoryOutbox,
    MemoryOutboxDelivery,
)
from app.repositories.memory import MemoryRepository
from app.services.embeddings import EmbeddingValidationError
from app.services.memory.contracts import MemoryLifecycleState
from app.services.memory.index_contracts import (
    DerivedFailureCode,
    DerivedTarget,
    DerivedTargetState,
    OutboxBatch,
    OutboxLease,
    OutboxProcessResult,
    OutboxTargetDiagnostic,
    RetryPolicy,
)
from app.services.memory.indexes import DerivedDocumentBuilder

_TERMINAL = {
    DerivedTargetState.CURRENT.value,
    DerivedTargetState.DELETED.value,
    DerivedTargetState.NOT_APPLICABLE.value,
}


def _stable_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, label))


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class MemoryOutboxProcessor:
    """Lease in short SQL transactions; call providers only after commit."""

    def __init__(
        self,
        engine: Engine,
        *,
        owner_id: str,
        database_identity: str,
        fts_index=None,
        vector_index=None,
        embedding_provider=None,
        retry_policy: RetryPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        after_target_write: Callable[[DerivedTarget, UUID], None] | None = None,
        jitter_source: Callable[[UUID, DerivedTarget, int], float] | None = None,
    ) -> None:
        self.engine = engine
        self.owner_id = str(UUID(owner_id))
        self.database_identity = database_identity
        self.fts_index = fts_index
        self.vector_index = vector_index
        self.embedding_provider = embedding_provider
        self.retry_policy = retry_policy or RetryPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.after_target_write = after_target_write
        self.jitter_source = jitter_source or self._stable_jitter
        self._sessions = sessionmaker(bind=engine, expire_on_commit=False)
        self.document_builder = DerivedDocumentBuilder()

    @property
    def enabled_targets(self) -> tuple[DerivedTarget, ...]:
        targets = []
        if self.fts_index is not None:
            targets.append(DerivedTarget.FTS)
        if self.vector_index is not None:
            targets.append(DerivedTarget.VECTOR)
        return tuple(targets)

    def lease_batch(
        self,
        *,
        worker_id: str,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
    ) -> OutboxBatch:
        now = self.clock()
        worker = worker_id.strip()
        if not worker or len(worker) > 120:
            raise ValueError("worker_id_invalid")
        if batch_size is not None and not 1 <= batch_size <= 500:
            raise ValueError("worker_batch_size_out_of_range")
        if lease_seconds is not None and not 5 <= lease_seconds <= 3_600:
            raise ValueError("worker_lease_seconds_out_of_range")
        batch = min(batch_size or self.retry_policy.batch_size, self.retry_policy.batch_size)
        duration = lease_seconds or self.retry_policy.lease_seconds
        leases: list[OutboxLease] = []
        with self._sessions.begin() as session:
            MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            events = list(
                session.scalars(
                    select(MemoryOutbox)
                    .where(
                        MemoryOutbox.owner_id == self.owner_id,
                        MemoryOutbox.event_kind.in_(
                            ("canonical_upsert", "canonical_remove", "reconciliation_request")
                        ),
                        MemoryOutbox.state.in_(("pending", "failed", "processing")),
                        or_(
                            MemoryOutbox.next_retry_at.is_(None),
                            MemoryOutbox.next_retry_at <= now,
                        ),
                    )
                    .order_by(MemoryOutbox.created_at, MemoryOutbox.id)
                    .limit(batch * 3)
                )
            )
            for event in events:
                targets = self._ensure_deliveries(session, event, now)
                leased_targets = []
                attempts = []
                for target in targets:
                    result = session.execute(
                        update(MemoryOutboxDelivery)
                        .where(
                            MemoryOutboxDelivery.id == target.id,
                            or_(
                                and_(
                                    MemoryOutboxDelivery.state.in_(
                                        (
                                            DerivedTargetState.PENDING.value,
                                            DerivedTargetState.FAILED.value,
                                            DerivedTargetState.STALE.value,
                                        )
                                    ),
                                    or_(
                                        MemoryOutboxDelivery.next_attempt_at.is_(None),
                                        MemoryOutboxDelivery.next_attempt_at <= now,
                                    ),
                                ),
                                and_(
                                    MemoryOutboxDelivery.state
                                    == DerivedTargetState.PROCESSING.value,
                                    MemoryOutboxDelivery.lease_expires_at <= now,
                                ),
                            ),
                        )
                        .values(
                            state=DerivedTargetState.PROCESSING.value,
                            attempts=MemoryOutboxDelivery.attempts + 1,
                            worker_id=worker,
                            leased_at=now,
                            lease_expires_at=now + timedelta(seconds=duration),
                            next_attempt_at=None,
                            last_error_code=None,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if result.rowcount == 1:
                        session.flush()
                        session.refresh(target)
                        leased_targets.append(DerivedTarget(target.target))
                        attempts.append(target.attempts)
                if leased_targets:
                    event.state = "processing"
                    event.attempts = max(event.attempts, max(attempts))
                    leases.append(
                        OutboxLease(
                            event_id=UUID(event.id),
                            owner_id=UUID(event.owner_id),
                            memory_id=UUID(event.memory_id) if event.memory_id else None,
                            event_kind=event.event_kind,
                            targets=tuple(leased_targets),
                            worker_id=worker,
                            leased_at=now,
                            lease_expires_at=now + timedelta(seconds=duration),
                            attempt=max(attempts),
                        )
                    )
                if len(leases) >= batch:
                    break
        return OutboxBatch(leases=tuple(leases))

    def process_batch(self, batch: OutboxBatch) -> tuple[OutboxProcessResult, ...]:
        return tuple(self.process(lease) for lease in batch.leases)

    def process(self, lease: OutboxLease) -> OutboxProcessResult:
        if str(lease.owner_id) != self.owner_id:
            raise ValueError(DerivedFailureCode.OWNER_BINDING_MISMATCH.value)
        event, record = self._load_event_and_record(lease)
        completed = []
        retryable = []
        dead = []
        failures = []
        diagnostics = []
        for target in lease.targets:
            started = monotonic()
            try:
                state, reason = self._process_target(event, record, target)
                if self.after_target_write is not None:
                    self.after_target_write(target, lease.event_id)
                self._finish_target(lease, target, state=state, error_code=reason)
                completed.append(target)
                diagnostics.append(
                    self._diagnostic(
                        lease,
                        event,
                        target,
                        state,
                        started,
                        repair_reason=reason,
                    )
                )
            except Exception as exc:
                code = self._failure_code(
                    target,
                    exc,
                    action=self._event_action(event),
                )
                is_dead = lease.attempt >= self.retry_policy.dead_letter_after
                self._set_derived_failure(
                    self._event_memory_id(event),
                    target,
                    (DerivedTargetState.DEAD_LETTER if is_dead else DerivedTargetState.FAILED),
                    revision=event.canonical_revision,
                    error_code=code.value,
                )
                self._finish_target(
                    lease,
                    target,
                    state=(
                        DerivedTargetState.DEAD_LETTER if is_dead else DerivedTargetState.FAILED
                    ),
                    error_code=code.value,
                )
                failures.append(code)
                (dead if is_dead else retryable).append(target)
                diagnostics.append(
                    self._diagnostic(
                        lease,
                        event,
                        target,
                        (DerivedTargetState.DEAD_LETTER if is_dead else DerivedTargetState.FAILED),
                        started,
                        failure_code=code,
                    )
                )
        return OutboxProcessResult(
            event_id=lease.event_id,
            completed_targets=tuple(completed),
            retryable_targets=tuple(retryable),
            dead_lettered_targets=tuple(dead),
            failure_codes=tuple(failures),
            diagnostics=tuple(diagnostics),
        )

    def _diagnostic(
        self,
        lease,
        event,
        target,
        state,
        started,
        *,
        failure_code=None,
        repair_reason=None,
    ):
        memory_id = self._event_memory_id(event)
        provider = self.embedding_provider if target is DerivedTarget.VECTOR else None
        if repair_reason is None and event.event_kind == "reconciliation_request":
            repair_reason = str((event.event_payload_json or {}).get("reason") or "") or None
        action = self._event_action(event)
        operation = (
            "not_applicable"
            if state is DerivedTargetState.NOT_APPLICABLE
            else ("delete" if action in {"canonical_remove", "delete"} else "upsert")
        )
        return OutboxTargetDiagnostic(
            event_id=lease.event_id,
            owner_id=UUID(self.owner_id),
            memory_id=UUID(memory_id) if memory_id else None,
            canonical_revision=event.canonical_revision,
            canonical_content_hash=(
                None if event.event_kind == "reconciliation_request" else event.content_hash
            ),
            expected_derived_hash=(
                event.content_hash if event.event_kind == "reconciliation_request" else None
            ),
            target=target,
            operation=operation,
            worker_id=lease.worker_id,
            attempt=lease.attempt,
            to_state=state,
            latency_ms=max(0, int((monotonic() - started) * 1_000)),
            provider=getattr(provider, "provider_name", None),
            model=getattr(provider, "model_name", None),
            provider_version=getattr(provider, "provider_version", None),
            failure_code=failure_code,
            repair_reason=repair_reason,
        )

    def requeue_dead_letter(self, event_id: UUID, target: DerivedTarget) -> bool:
        with self._sessions.begin() as session:
            result = session.execute(
                update(MemoryOutboxDelivery)
                .where(
                    MemoryOutboxDelivery.owner_id == self.owner_id,
                    MemoryOutboxDelivery.event_id == str(event_id),
                    MemoryOutboxDelivery.target == target.value,
                    MemoryOutboxDelivery.state == DerivedTargetState.DEAD_LETTER.value,
                )
                .values(
                    state=DerivedTargetState.PENDING.value,
                    attempts=0,
                    next_attempt_at=None,
                    worker_id=None,
                    leased_at=None,
                    lease_expires_at=None,
                    last_error_code=None,
                )
            )
            if result.rowcount:
                session.execute(
                    update(MemoryOutbox)
                    .where(
                        MemoryOutbox.owner_id == self.owner_id,
                        MemoryOutbox.id == str(event_id),
                    )
                    .values(state="pending", next_retry_at=None, last_error=None)
                )
            return bool(result.rowcount)

    def schedule_repair(self, request) -> None:
        """Queue an idempotent derived repair without mutating canonical state."""
        if str(request.owner_id) != self.owner_id:
            raise ValueError("owner_binding_mismatch")
        if request.action == "delete":
            self._queue_repair(
                memory_id=str(request.memory_id),
                action="canonical_remove",
                reason=request.reason,
                revision=None,
                content_hash=request.expected_hash,
                target=request.target,
            )
            return
        with self._sessions() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            record = repository.get_owner_record_any_lifecycle(str(request.memory_id))
            if record is None:
                return
            self._queue_repair(
                memory_id=record.id,
                action="upsert",
                reason=request.reason,
                revision=record.revision,
                content_hash=record.canonical_fingerprint,
                target=request.target,
            )

    def _ensure_deliveries(self, session, event, now):
        existing = {
            row.target: row
            for row in session.scalars(
                select(MemoryOutboxDelivery).where(
                    MemoryOutboxDelivery.owner_id == self.owner_id,
                    MemoryOutboxDelivery.event_id == event.id,
                )
            )
        }
        requested_target = (event.event_payload_json or {}).get("target")
        for target in self.enabled_targets:
            if requested_target is not None and target.value != requested_target:
                continue
            if target.value not in existing:
                row = MemoryOutboxDelivery(
                    id=_stable_id(f"delivery:{self.owner_id}:{event.id}:{target.value}"),
                    owner_id=self.owner_id,
                    event_id=event.id,
                    target=target.value,
                    state=DerivedTargetState.PENDING.value,
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                session.flush()
                existing[target.value] = row
        if not existing:
            event.state = "done"
            event.completed_at = now
        return tuple(existing.values())

    def _load_event_and_record(self, lease):
        with self._sessions() as session:
            repository = MemoryRepository(
                session,
                owner_id=self.owner_id,
                database_identity=self.database_identity,
            )
            event = session.scalar(
                select(MemoryOutbox).where(
                    MemoryOutbox.owner_id == self.owner_id,
                    MemoryOutbox.id == str(lease.event_id),
                )
            )
            if event is None:
                raise RuntimeError("lease_lost")
            record = None
            if event.memory_id:
                record = repository.get_record(
                    event.memory_id,
                    statuses=tuple(MemoryLifecycleState),
                )
                if record is not None:
                    session.expunge(record)
            session.expunge(event)
            return event, record

    def _process_target(self, event, record, target):
        action = event.event_kind
        memory_id = event.memory_id
        if action == "reconciliation_request":
            payload = event.event_payload_json or {}
            action = str(payload.get("action") or "")
            memory_id = str(payload.get("memory_id") or "") or memory_id
        if action == "canonical_remove":
            if not memory_id:
                raise RuntimeError("canonical_missing")
            derived = self._current_derived_metadata(memory_id, target)
            if (
                derived is not None
                and event.canonical_revision is not None
                and derived["canonical_revision"] is not None
                and derived["canonical_revision"] > event.canonical_revision
            ):
                return (
                    DerivedTargetState.NOT_APPLICABLE,
                    DerivedFailureCode.CANONICAL_HASH_ADVANCED.value,
                )
            repair_delete = event.event_kind == "reconciliation_request"
            expected = (
                event.content_hash
                if repair_delete and event.content_hash is not None
                else (derived["content_hash"] if derived is not None else None)
            )
            if (
                repair_delete
                and expected is not None
                and derived is not None
                and derived["content_hash"] != expected
            ):
                return (
                    DerivedTargetState.NOT_APPLICABLE,
                    DerivedFailureCode.CANONICAL_HASH_ADVANCED.value,
                )
            if target is DerivedTarget.FTS:
                deleted = self.fts_index.delete(self.owner_id, memory_id, expected)
            else:
                deleted = self.vector_index.delete(self.owner_id, memory_id, expected)
            if not deleted and derived is not None:
                remaining = self._current_derived_metadata(memory_id, target)
                if remaining is not None:
                    return (
                        DerivedTargetState.NOT_APPLICABLE,
                        DerivedFailureCode.CANONICAL_HASH_ADVANCED.value,
                    )
            self._set_derived_state(
                memory_id,
                target,
                DerivedTargetState.DELETED,
                content_hash=expected,
                revision=event.canonical_revision,
            )
            return DerivedTargetState.DELETED, None
        if action not in {"canonical_upsert", "upsert"}:
            return DerivedTargetState.NOT_APPLICABLE, "unsupported_event_kind"
        if record is None:
            self._schedule_delete(memory_id or "", "canonical_missing")
            return DerivedTargetState.DELETED, DerivedFailureCode.CANONICAL_MISSING.value
        now = self.clock()
        if record.status != MemoryLifecycleState.ACTIVE.value or (
            record.expires_at is not None
            and (
                record.expires_at.replace(tzinfo=UTC)
                if record.expires_at.tzinfo is None
                else record.expires_at.astimezone(UTC)
            )
            <= now.astimezone(UTC)
        ):
            self._schedule_delete(record.id, "canonical_inactive")
            return DerivedTargetState.DELETED, DerivedFailureCode.CANONICAL_INACTIVE.value
        if event.event_kind == "canonical_upsert" and (
            event.canonical_revision != record.revision
            or event.content_hash != record.canonical_fingerprint
        ):
            self._schedule_upsert(record, "canonical_hash_advanced")
            return (
                DerivedTargetState.NOT_APPLICABLE,
                DerivedFailureCode.CANONICAL_HASH_ADVANCED.value,
            )
        document = self.document_builder.build(record, now=now)
        if document is None:
            existing = self._current_derived_metadata(record.id, target)
            expected = existing["content_hash"] if existing is not None else None
            if target is DerivedTarget.FTS:
                self.fts_index.delete(self.owner_id, record.id, expected)
            else:
                self.vector_index.delete(self.owner_id, record.id, expected)
            self._set_derived_state(
                record.id,
                target,
                DerivedTargetState.NOT_APPLICABLE,
                content_hash=None,
                revision=record.revision,
            )
            return DerivedTargetState.NOT_APPLICABLE, "policy_not_applicable"
        if target is DerivedTarget.FTS:
            self.fts_index.upsert(document)
            provider = None
        else:
            if self.embedding_provider is None:
                raise RuntimeError(DerivedFailureCode.EMBEDDING_UNAVAILABLE.value)
            embedding_document = self.document_builder.build_embedding(document)
            vector = self.embedding_provider.embed(embedding_document.text)
            self.vector_index.upsert(
                document,
                vector,
                self.embedding_provider,
                embedding_document=embedding_document,
            )
            provider = self.embedding_provider
        self._set_derived_state(
            record.id,
            target,
            DerivedTargetState.CURRENT,
            content_hash=document.content_hash,
            revision=record.revision,
            provider=provider,
        )
        return DerivedTargetState.CURRENT, None

    def _set_derived_state(
        self,
        memory_id,
        target,
        state,
        *,
        content_hash,
        revision,
        provider=None,
    ):
        now = self.clock()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryDerivedState).where(
                    MemoryDerivedState.owner_id == self.owner_id,
                    MemoryDerivedState.memory_id == memory_id,
                    MemoryDerivedState.target == target.value,
                )
            )
            values = {
                "state": state.value,
                "content_hash": content_hash,
                "canonical_revision": revision,
                "provider": getattr(provider, "provider_name", None),
                "model": getattr(provider, "model_name", None),
                "provider_version": getattr(provider, "provider_version", None),
                "dimension": getattr(provider, "dimension", None),
                "last_error_code": None,
                "updated_at": now,
            }
            if row is None:
                session.add(
                    MemoryDerivedState(
                        id=_stable_id(f"derived:{self.owner_id}:{memory_id}:{target.value}"),
                        owner_id=self.owner_id,
                        memory_id=memory_id,
                        target=target.value,
                        **values,
                    )
                )
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def _set_derived_failure(self, memory_id, target, state, *, revision, error_code) -> None:
        if not memory_id:
            return
        now = self.clock()
        with self._sessions.begin() as session:
            row = session.scalar(
                select(MemoryDerivedState).where(
                    MemoryDerivedState.owner_id == self.owner_id,
                    MemoryDerivedState.memory_id == memory_id,
                    MemoryDerivedState.target == target.value,
                )
            )
            if row is None:
                session.add(
                    MemoryDerivedState(
                        id=_stable_id(f"derived:{self.owner_id}:{memory_id}:{target.value}"),
                        owner_id=self.owner_id,
                        memory_id=memory_id,
                        target=target.value,
                        state=state.value,
                        canonical_revision=revision,
                        last_error_code=error_code,
                        updated_at=now,
                    )
                )
            else:
                row.state = state.value
                row.canonical_revision = revision or row.canonical_revision
                row.last_error_code = error_code
                row.updated_at = now

    @staticmethod
    def _event_memory_id(event):
        if event.memory_id:
            return event.memory_id
        return str((event.event_payload_json or {}).get("memory_id") or "") or None

    @staticmethod
    def _event_action(event):
        if event.event_kind == "reconciliation_request":
            return str((event.event_payload_json or {}).get("action") or "")
        return event.event_kind

    def _finish_target(self, lease, target, *, state, error_code):
        now = self.clock()
        with self._sessions.begin() as session:
            delivery = session.scalar(
                select(MemoryOutboxDelivery).where(
                    MemoryOutboxDelivery.owner_id == self.owner_id,
                    MemoryOutboxDelivery.event_id == str(lease.event_id),
                    MemoryOutboxDelivery.target == target.value,
                    MemoryOutboxDelivery.state == DerivedTargetState.PROCESSING.value,
                    MemoryOutboxDelivery.worker_id == lease.worker_id,
                )
            )
            if delivery is None:
                raise RuntimeError("lease_lost")
            delivery.state = state.value
            delivery.last_error_code = error_code
            delivery.worker_id = None
            delivery.leased_at = None
            delivery.lease_expires_at = None
            if state is DerivedTargetState.FAILED:
                delivery.next_attempt_at = now + timedelta(
                    seconds=self.retry_policy.delay_for(
                        delivery.attempts,
                        jitter_fraction=self.jitter_source(
                            lease.event_id, target, delivery.attempts
                        ),
                    )
                )
            else:
                delivery.next_attempt_at = None
            if state.value in _TERMINAL:
                delivery.completed_at = now
            self._refresh_event_state(session, str(lease.event_id), now)

    def _refresh_event_state(self, session, event_id, now):
        event = session.scalar(
            select(MemoryOutbox).where(
                MemoryOutbox.owner_id == self.owner_id,
                MemoryOutbox.id == event_id,
            )
        )
        deliveries = list(
            session.scalars(
                select(MemoryOutboxDelivery).where(
                    MemoryOutboxDelivery.owner_id == self.owner_id,
                    MemoryOutboxDelivery.event_id == event_id,
                )
            )
        )
        if deliveries and all(item.state in _TERMINAL for item in deliveries):
            event.state = "done"
            event.completed_at = now
            event.next_retry_at = None
            event.last_error = None
        elif any(item.state == DerivedTargetState.DEAD_LETTER.value for item in deliveries):
            event.state = "failed"
            event.next_retry_at = None
            event.last_error = "dead_letter"
        elif any(item.state == DerivedTargetState.FAILED.value for item in deliveries):
            event.state = "failed"
            retries = [item.next_attempt_at for item in deliveries if item.next_attempt_at]
            event.next_retry_at = min(_aware(item) for item in retries) if retries else None
            event.last_error = "derived_target_failed"
        else:
            event.state = "processing"

    def _current_derived_metadata(self, memory_id, target):
        with self._sessions() as session:
            row = session.scalar(
                select(MemoryDerivedState).where(
                    MemoryDerivedState.owner_id == self.owner_id,
                    MemoryDerivedState.memory_id == memory_id,
                    MemoryDerivedState.target == target.value,
                )
            )
            derived = (
                None
                if row is None
                else {
                    "content_hash": row.content_hash,
                    "canonical_revision": row.canonical_revision,
                    "state": row.state,
                }
            )
        adapter = self.fts_index if target is DerivedTarget.FTS else self.vector_index
        indexed = adapter.get_metadata(self.owner_id, memory_id)
        if indexed is None:
            return derived
        if derived is None:
            return indexed
        indexed_revision = indexed.get("canonical_revision")
        derived_revision = derived.get("canonical_revision")
        revisions = [value for value in (indexed_revision, derived_revision) if value is not None]
        return {
            **indexed,
            "canonical_revision": max(revisions) if revisions else None,
            "state": derived.get("state"),
        }

    def _schedule_upsert(self, record, reason):
        self._queue_repair(
            memory_id=record.id,
            action="upsert",
            reason=reason,
            revision=record.revision,
            content_hash=record.canonical_fingerprint,
            target=None,
        )

    def _schedule_delete(self, memory_id, reason):
        if memory_id:
            self._queue_repair(
                memory_id=memory_id,
                action="canonical_remove",
                reason=reason,
                revision=None,
                content_hash=None,
                target=None,
            )

    def _queue_repair(self, *, memory_id, action, reason, revision, content_hash, target):
        target_value = target.value if target is not None else "all"
        key = (
            f"memory:repair:{self.owner_id}:{memory_id}:{action}:"
            f"{target_value}:{content_hash or 'none'}"
        )
        now = self.clock()
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(MemoryOutbox).where(
                    MemoryOutbox.owner_id == self.owner_id,
                    MemoryOutbox.event_idempotency_key == key,
                )
            )
            if existing is not None:
                if existing.state == "done":
                    existing.state = "pending"
                    existing.completed_at = None
                    existing.next_retry_at = None
                    existing.last_error = None
                    for delivery in session.scalars(
                        select(MemoryOutboxDelivery).where(
                            MemoryOutboxDelivery.owner_id == self.owner_id,
                            MemoryOutboxDelivery.event_id == existing.id,
                        )
                    ):
                        delivery.state = DerivedTargetState.PENDING.value
                        delivery.attempts = 0
                        delivery.completed_at = None
                        delivery.next_attempt_at = None
                return
            session.add(
                MemoryOutbox(
                    id=_stable_id(key),
                    owner_id=self.owner_id,
                    event_kind=(
                        "canonical_upsert" if action == "upsert" else "reconciliation_request"
                    ),
                    memory_id=memory_id if action == "upsert" else None,
                    canonical_revision=revision,
                    content_hash=content_hash,
                    event_payload_json={
                        "action": action,
                        "memory_id": memory_id,
                        "reason": reason,
                        "target": target.value if target is not None else None,
                    },
                    state="pending",
                    attempts=0,
                    event_idempotency_key=key,
                    created_at=now,
                    updated_at=now,
                )
            )

    @staticmethod
    def _stable_jitter(event_id: UUID, target: DerivedTarget, attempt: int) -> float:
        material = f"{event_id}:{target.value}:{attempt}".encode()
        return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") / (2**64 - 1)

    @staticmethod
    def _failure_code(target, exc, *, action):
        if isinstance(exc, EmbeddingValidationError):
            try:
                return DerivedFailureCode(exc.code)
            except ValueError:
                return DerivedFailureCode.EMBEDDING_INVALID_RESPONSE
        message = str(exc)
        if message == "lease_lost":
            return DerivedFailureCode.LEASE_LOST
        if message == "owner_binding_mismatch":
            return DerivedFailureCode.OWNER_BINDING_MISMATCH
        if message == "vector_unavailable":
            return DerivedFailureCode.VECTOR_UNAVAILABLE
        if message == DerivedFailureCode.EMBEDDING_UNAVAILABLE.value:
            return DerivedFailureCode.EMBEDDING_UNAVAILABLE
        if target is DerivedTarget.FTS:
            return (
                DerivedFailureCode.FTS_DELETE_FAILED
                if action in {"canonical_remove", "delete"}
                else DerivedFailureCode.FTS_UPSERT_FAILED
            )
        if action in {"canonical_remove", "delete"}:
            return DerivedFailureCode.VECTOR_DELETE_FAILED
        return DerivedFailureCode.VECTOR_UPSERT_FAILED
