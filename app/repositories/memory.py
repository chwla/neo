"""Owner-bound persistence and canonical serving primitives for personal memory."""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from datetime import datetime
from typing import Any, TypeVar
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.identifiers import canonical_uuid
from app.models.memory import (
    MemoryCandidate,
    MemoryOperation,
    MemoryOutbox,
    MemoryOwnerBinding,
    MemoryRecord,
    MemoryRelation,
    MemorySource,
    MemoryTombstone,
    MemoryUsageEvent,
)
from app.services.memory.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory.policy import classify_sensitivity
from app.services.memory.taxonomy import MemoryType

_UUID_TEXT_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])"
)

ALLOWED_RECORD_METADATA_KEYS = frozenset({"tags", "user_label", "review_note"})
_UPDATABLE_RECORD_FIELDS = frozenset(
    {
        "canonical_payload",
        "display_text",
        "encrypted_canonical_payload",
        "encrypted_display_payload",
        "encryption_algorithm",
        "encryption_key_version",
        "canonical_nonce",
        "display_nonce",
        "encryption_aad",
        "sensitivity",
        "canonical_fingerprint",
        "confidence",
        "importance",
        "status",
        "last_confirmed_at",
        "expires_at",
        "last_used_at",
        "usage_count",
        "pinned",
        "metadata_json",
    }
)
_UPDATABLE_CANDIDATE_FIELDS = frozenset(
    {
        "state",
        "decision_outcome",
        "decision_rejection_code",
        "decision_error_code",
        "decision_reason",
        "applied_operation_id",
        "decided_at",
    }
)

_OwnedModel = TypeVar(
    "_OwnedModel",
    MemoryOperation,
    MemoryRecord,
    MemoryCandidate,
    MemorySource,
    MemoryRelation,
    MemoryOutbox,
    MemoryTombstone,
    MemoryUsageEvent,
)


class MemoryRepositoryError(RuntimeError):
    pass


class MemoryBindingError(MemoryRepositoryError):
    pass


class MemoryNotFoundError(MemoryRepositoryError):
    """Owner-safe not-found error used for missing and cross-owner references."""


class MemoryRevisionConflict(MemoryRepositoryError):
    pass


class MemoryProhibitedContentError(MemoryRepositoryError):
    pass


class MemoryRepository:
    def __init__(self, session: Session, *, owner_id: str, database_identity: str) -> None:
        self._session = session
        self.owner_id = canonical_uuid(owner_id)
        if not database_identity.strip():
            raise MemoryBindingError("database_identity_required")
        self.database_identity = database_identity
        self._validate_binding()

    def _validate_binding(self) -> None:
        try:
            rows = self._session.scalars(select(MemoryOwnerBinding)).all()
        except SQLAlchemyError as exc:
            raise MemoryBindingError("memory_schema_or_binding_unavailable") from exc
        if len(rows) != 1:
            raise MemoryBindingError("memory_database_requires_one_owner_binding")
        binding = rows[0]
        if binding.owner_id != self.owner_id or binding.database_identity != self.database_identity:
            raise MemoryBindingError("memory_owner_database_binding_mismatch")

    def _require_owned(self, entity: _OwnedModel) -> _OwnedModel:
        try:
            entity_owner = canonical_uuid(entity.owner_id)
        except ValueError as exc:
            raise MemoryBindingError("entity_owner_id_invalid") from exc
        if entity_owner != self.owner_id:
            raise MemoryNotFoundError("owner_bound_reference_not_found")
        entity.owner_id = entity_owner
        return entity

    def _record_exists(self, memory_id: str) -> bool:
        record_id = canonical_uuid(memory_id)
        statement = select(MemoryRecord.id).where(
            MemoryRecord.owner_id == self.owner_id,
            MemoryRecord.id == record_id,
        )
        return self._session.scalar(statement) is not None

    def _operation_exists(self, operation_id: str) -> bool:
        identifier = canonical_uuid(operation_id)
        statement = select(MemoryOperation.id).where(
            MemoryOperation.owner_id == self.owner_id,
            MemoryOperation.id == identifier,
        )
        return self._session.scalar(statement) is not None

    @staticmethod
    def _reject_prohibited_material(*values: Any) -> None:
        material = "\n".join(
            json.dumps(value, sort_keys=True, default=str) for value in values if value is not None
        )
        material = _UUID_TEXT_PATTERN.sub("<uuid>", material)
        if material and classify_sensitivity(material) is Sensitivity.PROHIBITED:
            raise MemoryProhibitedContentError("prohibited_content_not_persisted")

    @staticmethod
    def _validated_statuses(
        statuses: Collection[MemoryLifecycleState | str],
    ) -> tuple[str, ...]:
        if not statuses:
            raise ValueError("explicit_status_filter_required")
        allowed = {state.value for state in MemoryLifecycleState}
        values = tuple(
            state.value if isinstance(state, MemoryLifecycleState) else state for state in statuses
        )
        if not set(values) <= allowed:
            raise ValueError("invalid_memory_status_filter")
        return values

    def get_record(
        self,
        memory_id: str,
        *,
        statuses: Collection[MemoryLifecycleState | str],
    ) -> MemoryRecord | None:
        identifier = canonical_uuid(memory_id)
        values = self._validated_statuses(statuses)
        return self._session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.id == identifier,
                MemoryRecord.status.in_(values),
            )
        )

    def eligible_records_statement(
        self,
        *,
        now: datetime,
        memory_types: Collection[MemoryType | str] = (),
        domain_keys: Collection[str] = (),
    ) -> Select[tuple[MemoryRecord]]:
        """Build the authoritative normal-serving query with SQL-level eligibility."""
        statement = select(MemoryRecord).where(
            MemoryRecord.owner_id == self.owner_id,
            MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
            or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
        )
        if memory_types:
            values = tuple(
                item.value if isinstance(item, MemoryType) else MemoryType(item).value
                for item in memory_types
            )
            statement = statement.where(MemoryRecord.memory_type.in_(values))
        if domain_keys:
            statement = statement.where(MemoryRecord.domain_key.in_(tuple(domain_keys)))
        return statement

    def recall_filter_counts(
        self,
        *,
        now: datetime,
        memory_id: str | None = None,
        memory_types: Collection[MemoryType | str] = (),
        domain_keys: Collection[str] = (),
        slot_keys: Collection[str] = (),
    ) -> tuple[int, int]:
        """Count owner-bound inactive and expired rows excluded by serving SQL."""
        conditions = [MemoryRecord.owner_id == self.owner_id]
        if memory_id is not None:
            conditions.append(MemoryRecord.id == canonical_uuid(memory_id))
        if memory_types:
            values = tuple(
                item.value if isinstance(item, MemoryType) else MemoryType(item).value
                for item in memory_types
            )
            conditions.append(MemoryRecord.memory_type.in_(values))
        if domain_keys:
            conditions.append(MemoryRecord.domain_key.in_(tuple(domain_keys)))
        normalized_slots = tuple(item.strip() for item in slot_keys if item.strip())
        if normalized_slots:
            conditions.append(MemoryRecord.slot_key.in_(normalized_slots))
        inactive = self._session.scalar(
            select(func.count())
            .select_from(MemoryRecord)
            .where(
                *conditions,
                MemoryRecord.status != MemoryLifecycleState.ACTIVE.value,
            )
        )
        expired = self._session.scalar(
            select(func.count())
            .select_from(MemoryRecord)
            .where(
                *conditions,
                MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= now,
            )
        )
        return int(inactive or 0), int(expired or 0)

    def list_recall_eligible(
        self,
        *,
        now: datetime,
        memory_types: Collection[MemoryType | str] = (),
        domain_keys: Collection[str] = (),
        limit: int = 500,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= 500:
            raise ValueError("recall_candidate_limit_out_of_range")
        statement = self.eligible_records_statement(
            now=now,
            memory_types=memory_types,
            domain_keys=domain_keys,
        ).order_by(
            MemoryRecord.last_confirmed_at.desc(),
            MemoryRecord.updated_at.desc(),
            MemoryRecord.id.asc(),
        )
        return list(self._session.scalars(statement.limit(limit)))

    def get_recall_eligible_by_id(
        self,
        memory_id: str,
        *,
        now: datetime,
    ) -> MemoryRecord | None:
        identifier = canonical_uuid(memory_id)
        return self._session.scalar(
            self.eligible_records_statement(now=now).where(MemoryRecord.id == identifier)
        )

    def get_owner_record_any_lifecycle(self, memory_id: str) -> MemoryRecord | None:
        """Owner-bound validation read used only to classify untrusted derived hits."""
        identifier = canonical_uuid(memory_id)
        return self._session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.id == identifier,
            )
        )

    def list_index_candidates(
        self,
        *,
        now: datetime,
        after_memory_id: str | None = None,
        limit: int = 500,
    ) -> list[MemoryRecord]:
        """Bounded owner-scoped canonical enumeration for derived maintenance."""
        if not 1 <= limit <= 1_001:
            raise ValueError("index_candidate_limit_out_of_range")
        statement = self.eligible_records_statement(now=now).order_by(MemoryRecord.id)
        if after_memory_id is not None:
            statement = statement.where(MemoryRecord.id > canonical_uuid(after_memory_id))
        return list(self._session.scalars(statement.limit(limit)))

    def find_recall_eligible_slot(
        self,
        *,
        now: datetime,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
    ) -> MemoryRecord | None:
        statement = self.eligible_records_statement(
            now=now,
            memory_types=(memory_type,),
            domain_keys=(domain_key,),
        ).where(MemoryRecord.slot_key == slot_key)
        return self._session.scalar(
            statement.order_by(
                MemoryRecord.last_confirmed_at.desc(),
                MemoryRecord.id.asc(),
            ).limit(1)
        )

    def list_recall_eligible_for_slots(
        self,
        slot_keys: Collection[str],
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        slots = tuple(dict.fromkeys(item.strip() for item in slot_keys if item.strip()))
        if not slots:
            return []
        if len(slots) > 50 or not 1 <= limit <= 100:
            raise ValueError("trusted_slot_query_out_of_range")
        statement = self.eligible_records_statement(now=now).where(MemoryRecord.slot_key.in_(slots))
        return list(
            self._session.scalars(
                statement.order_by(
                    MemoryRecord.last_confirmed_at.desc(),
                    MemoryRecord.id.asc(),
                ).limit(limit)
            )
        )

    def active_source_ids_for_records(
        self,
        memory_ids: Collection[str],
    ) -> dict[str, tuple[str, ...]]:
        identifiers = tuple(dict.fromkeys(canonical_uuid(item) for item in memory_ids))
        if not identifiers:
            return {}
        rows = self._session.execute(
            select(MemorySource.memory_id, MemorySource.id).where(
                MemorySource.owner_id == self.owner_id,
                MemorySource.memory_id.in_(identifiers),
                MemorySource.is_active.is_(True),
            )
        ).all()
        grouped: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
        for memory_id, source_id in rows:
            grouped[memory_id].append(source_id)
        return {memory_id: tuple(sorted(source_ids)) for memory_id, source_ids in grouped.items()}

    def record_recall_usage(
        self,
        memory_ids: Collection[str],
        *,
        used_at: datetime,
        request_id: str = "unspecified",
        session_id: str = "unspecified",
        purpose: str = "recall",
    ) -> tuple[str, ...]:
        identifiers = tuple(dict.fromkeys(canonical_uuid(item) for item in memory_ids))
        if not identifiers:
            return ()
        with self._session.begin_nested() as savepoint:
            result = self._session.execute(
                update(MemoryRecord)
                .where(
                    MemoryRecord.owner_id == self.owner_id,
                    MemoryRecord.id.in_(identifiers),
                    MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
                    or_(
                        MemoryRecord.expires_at.is_(None),
                        MemoryRecord.expires_at > used_at,
                    ),
                )
                .values(
                    usage_count=MemoryRecord.usage_count + 1,
                    last_used_at=used_at,
                )
            )
            if result.rowcount != len(identifiers):
                savepoint.rollback()
                raise MemoryNotFoundError("usage_selection_not_fully_eligible")
        self._session.flush()
        for memory_id in identifiers:
            event_id = str(
                uuid5(
                    NAMESPACE_URL,
                    f"neo.memory.usage:{self.owner_id}:{request_id}:{memory_id}:{purpose}",
                )
            )
            if self._session.get(MemoryUsageEvent, event_id) is None:
                self._session.add(
                    MemoryUsageEvent(
                        id=event_id,
                        owner_id=self.owner_id,
                        memory_id=memory_id,
                        request_id=request_id,
                        session_id=session_id,
                        purpose=purpose,
                        used_at=used_at,
                    )
                )
        self._session.flush()
        return identifiers

    def list_records(
        self,
        *,
        statuses: Collection[MemoryLifecycleState | str],
        memory_type: MemoryType | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        if not 1 <= limit <= 1_000:
            raise ValueError("record_limit_out_of_range")
        values = self._validated_statuses(statuses)
        statement: Select[tuple[MemoryRecord]] = select(MemoryRecord).where(
            MemoryRecord.owner_id == self.owner_id,
            MemoryRecord.status.in_(values),
        )
        if memory_type is not None:
            statement = statement.where(MemoryRecord.memory_type == memory_type.value)
        statement = statement.order_by(MemoryRecord.created_at.desc()).limit(limit)
        return list(self._session.scalars(statement))

    def find_active_slot(
        self,
        *,
        subject_key: str,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
    ) -> list[MemoryRecord]:
        statement = select(MemoryRecord).where(
            MemoryRecord.owner_id == self.owner_id,
            MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
            MemoryRecord.subject_key == subject_key,
            MemoryRecord.memory_type == memory_type.value,
            MemoryRecord.domain_key == domain_key,
            MemoryRecord.slot_key == slot_key,
        )
        return list(self._session.scalars(statement))

    def find_active_fingerprint(self, fingerprint: str) -> MemoryRecord | None:
        return self._session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.status == MemoryLifecycleState.ACTIVE.value,
                MemoryRecord.canonical_fingerprint == fingerprint,
            )
        )

    def get_operation_by_idempotency_key(self, key: str) -> MemoryOperation | None:
        return self._session.scalar(
            select(MemoryOperation).where(
                MemoryOperation.owner_id == self.owner_id,
                MemoryOperation.idempotency_key == key,
            )
        )

    def add_operation(self, operation: MemoryOperation) -> MemoryOperation:
        self._require_owned(operation)
        self._reject_prohibited_material(operation.normalized_command_json, operation.error_detail)
        self._session.add(operation)
        self._session.flush()
        return operation

    def add_record(self, record: MemoryRecord) -> MemoryRecord:
        self._require_owned(record)
        self._reject_prohibited_material(
            record.canonical_payload,
            record.display_text,
            record.metadata_json,
        )
        if not self._operation_exists(record.created_by_operation_id):
            raise MemoryNotFoundError("creating_operation_not_found")
        unknown_metadata = set(record.metadata_json or {}) - ALLOWED_RECORD_METADATA_KEYS
        if unknown_metadata:
            raise ValueError(
                f"record_metadata_keys_not_allowed:{','.join(sorted(unknown_metadata))}"
            )
        self._session.add(record)
        self._session.flush()
        return record

    def add_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        self._require_owned(candidate)
        self._reject_prohibited_material(
            candidate.canonical_payload,
            candidate.display_text,
            candidate.decision_reason,
        )
        for target_id in candidate.trusted_target_ids or []:
            if not self._record_exists(target_id):
                raise MemoryNotFoundError("candidate_target_not_found")
        self._session.add(candidate)
        self._session.flush()
        return candidate

    def get_candidate(self, candidate_id: str) -> MemoryCandidate | None:
        identifier = canonical_uuid(candidate_id)
        return self._session.scalar(
            select(MemoryCandidate).where(
                MemoryCandidate.owner_id == self.owner_id,
                MemoryCandidate.id == identifier,
            )
        )

    def update_candidate_decision(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> MemoryCandidate:
        identifier = canonical_uuid(candidate_id)
        if expected_revision < 1:
            raise ValueError("expected_revision_must_be_positive")
        if not values:
            raise ValueError("candidate_update_requires_values")
        unknown = set(values) - _UPDATABLE_CANDIDATE_FIELDS
        if unknown:
            raise ValueError(f"candidate_update_fields_not_allowed:{','.join(sorted(unknown))}")
        operation_id = values.get("applied_operation_id")
        if operation_id is not None and not self._operation_exists(operation_id):
            raise MemoryNotFoundError("candidate_operation_not_found")
        self._reject_prohibited_material(values.get("decision_reason"))
        result = self._session.execute(
            update(MemoryCandidate)
            .where(
                MemoryCandidate.owner_id == self.owner_id,
                MemoryCandidate.id == identifier,
                MemoryCandidate.revision == expected_revision,
            )
            .values(
                **values,
                revision=MemoryCandidate.revision + 1,
                updated_at=func.now(),
            )
        )
        if result.rowcount != 1:
            raise MemoryRevisionConflict("candidate_revision_conflict_or_not_found")
        self._session.flush()
        candidate = self.get_candidate(identifier)
        if candidate is None:
            raise MemoryNotFoundError("candidate_not_found")
        return candidate

    def add_source(self, source: MemorySource) -> MemorySource:
        self._require_owned(source)
        self._reject_prohibited_material(source.redacted_excerpt)
        if not self._record_exists(source.memory_id) or not self._operation_exists(
            source.operation_id
        ):
            raise MemoryNotFoundError("source_reference_not_found")
        # A reconfirmation may cite the exact same provenance material as the
        # existing assertion.  Provenance is deduplicated by this identity, so
        # acknowledge it rather than turning an otherwise valid reconfirmation
        # into a transaction failure.
        existing = self._session.scalar(
            select(MemorySource).where(
                MemorySource.owner_id == self.owner_id,
                MemorySource.memory_id == source.memory_id,
                MemorySource.source_content_hash == source.source_content_hash,
            )
        )
        if existing is not None:
            return existing
        self._session.add(source)
        self._session.flush()
        return source

    def add_relation(self, relation: MemoryRelation) -> MemoryRelation:
        self._require_owned(relation)
        if not self._record_exists(relation.from_memory_id) or not self._record_exists(
            relation.to_memory_id
        ):
            raise MemoryNotFoundError("relation_endpoint_not_found")
        if not self._operation_exists(relation.operation_id):
            raise MemoryNotFoundError("relation_operation_not_found")
        self._session.add(relation)
        self._session.flush()
        return relation

    def add_outbox_event(self, event: MemoryOutbox) -> MemoryOutbox:
        self._require_owned(event)
        self._reject_prohibited_material(event.event_payload_json, event.last_error)
        if event.memory_id is not None and not self._record_exists(event.memory_id):
            raise MemoryNotFoundError("outbox_memory_not_found")
        self._session.add(event)
        self._session.flush()
        return event

    def add_tombstone(self, tombstone: MemoryTombstone) -> MemoryTombstone:
        self._require_owned(tombstone)
        if not self._operation_exists(tombstone.originating_operation_id):
            raise MemoryNotFoundError("tombstone_operation_not_found")
        self._session.add(tombstone)
        self._session.flush()
        return tombstone

    def update_record_fields(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> MemoryRecord:
        identifier = canonical_uuid(memory_id)
        if expected_revision < 1:
            raise ValueError("expected_revision_must_be_positive")
        if not values:
            raise ValueError("record_update_requires_values")
        unknown = set(values) - _UPDATABLE_RECORD_FIELDS
        if unknown:
            raise ValueError(f"record_update_fields_not_allowed:{','.join(sorted(unknown))}")
        if "metadata_json" in values:
            unknown_metadata = set(values["metadata_json"] or {}) - ALLOWED_RECORD_METADATA_KEYS
            if unknown_metadata:
                raise ValueError(
                    f"record_metadata_keys_not_allowed:{','.join(sorted(unknown_metadata))}"
                )
        self._reject_prohibited_material(*values.values())

        statement = (
            update(MemoryRecord)
            .where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.id == identifier,
                MemoryRecord.revision == expected_revision,
            )
            .values(
                **values,
                revision=MemoryRecord.revision + 1,
                updated_at=func.now(),
            )
        )
        result = self._session.execute(statement)
        if result.rowcount != 1:
            raise MemoryRevisionConflict("record_revision_conflict_or_not_found")
        self._session.flush()
        record = self._session.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == self.owner_id,
                MemoryRecord.id == identifier,
            )
        )
        if record is None:
            raise MemoryNotFoundError("record_not_found")
        return record

    def delete_tombstone(self, tombstone_id: str) -> bool:
        identifier = canonical_uuid(tombstone_id)
        tombstone = self._session.scalar(
            select(MemoryTombstone).where(
                MemoryTombstone.owner_id == self.owner_id,
                MemoryTombstone.id == identifier,
            )
        )
        if tombstone is None:
            return False
        self._session.delete(tombstone)
        self._session.flush()
        return True
