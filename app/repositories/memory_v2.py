"""Owner-bound persistence and canonical serving primitives for memory v2.

Lifecycle and extraction decisions remain in their services. Phase 5 adds only
SQL-level eligibility, source tracing, and usage metadata operations here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.identifiers import canonical_uuid
from app.models.memory_v2 import (
    MemoryCandidateV2,
    MemoryLegacyMapV2,
    MemoryMigrationRunV2,
    MemoryOperationV2,
    MemoryOutboxV2,
    MemoryOwnerBindingV2,
    MemoryRecordV2,
    MemoryRelationV2,
    MemorySourceV2,
    MemoryTombstoneV2,
)
from app.services.memory_v2.contracts import MemoryLifecycleState, Sensitivity
from app.services.memory_v2.policy import classify_sensitivity
from app.services.memory_v2.taxonomy import MemoryType

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
    MemoryOperationV2,
    MemoryRecordV2,
    MemoryCandidateV2,
    MemorySourceV2,
    MemoryRelationV2,
    MemoryOutboxV2,
    MemoryTombstoneV2,
    MemoryLegacyMapV2,
    MemoryMigrationRunV2,
)


class MemoryV2RepositoryError(RuntimeError):
    pass


class MemoryV2BindingError(MemoryV2RepositoryError):
    pass


class MemoryV2NotFoundError(MemoryV2RepositoryError):
    """Owner-safe not-found error used for missing and cross-owner references."""


class MemoryV2RevisionConflict(MemoryV2RepositoryError):
    pass


class MemoryV2ProhibitedContentError(MemoryV2RepositoryError):
    pass


class MemoryV2Repository:
    def __init__(self, session: Session, *, owner_id: str, database_identity: str) -> None:
        self._session = session
        self.owner_id = canonical_uuid(owner_id)
        if not database_identity.strip():
            raise MemoryV2BindingError("database_identity_required")
        self.database_identity = database_identity
        self._validate_binding()

    def _validate_binding(self) -> None:
        try:
            rows = self._session.scalars(select(MemoryOwnerBindingV2)).all()
        except SQLAlchemyError as exc:
            raise MemoryV2BindingError("memory_v2_schema_or_binding_unavailable") from exc
        if len(rows) != 1:
            raise MemoryV2BindingError("memory_v2_database_requires_one_owner_binding")
        binding = rows[0]
        if binding.owner_id != self.owner_id or binding.database_identity != self.database_identity:
            raise MemoryV2BindingError("memory_v2_owner_database_binding_mismatch")

    def _require_owned(self, entity: _OwnedModel) -> _OwnedModel:
        try:
            entity_owner = canonical_uuid(entity.owner_id)
        except ValueError as exc:
            raise MemoryV2BindingError("entity_owner_id_invalid") from exc
        if entity_owner != self.owner_id:
            raise MemoryV2NotFoundError("owner_bound_reference_not_found")
        entity.owner_id = entity_owner
        return entity

    def _record_exists(self, memory_id: str) -> bool:
        record_id = canonical_uuid(memory_id)
        statement = select(MemoryRecordV2.id).where(
            MemoryRecordV2.owner_id == self.owner_id,
            MemoryRecordV2.id == record_id,
        )
        return self._session.scalar(statement) is not None

    def _operation_exists(self, operation_id: str) -> bool:
        identifier = canonical_uuid(operation_id)
        statement = select(MemoryOperationV2.id).where(
            MemoryOperationV2.owner_id == self.owner_id,
            MemoryOperationV2.id == identifier,
        )
        return self._session.scalar(statement) is not None

    def _migration_run_exists(self, run_id: str) -> bool:
        identifier = canonical_uuid(run_id)
        statement = select(MemoryMigrationRunV2.id).where(
            MemoryMigrationRunV2.owner_id == self.owner_id,
            MemoryMigrationRunV2.id == identifier,
        )
        return self._session.scalar(statement) is not None

    @staticmethod
    def _reject_prohibited_material(*values: Any) -> None:
        material = "\n".join(
            json.dumps(value, sort_keys=True, default=str) for value in values if value is not None
        )
        material = _UUID_TEXT_PATTERN.sub("<uuid>", material)
        if material and classify_sensitivity(material) is Sensitivity.PROHIBITED:
            raise MemoryV2ProhibitedContentError("prohibited_content_not_persisted")

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
    ) -> MemoryRecordV2 | None:
        identifier = canonical_uuid(memory_id)
        values = self._validated_statuses(statuses)
        return self._session.scalar(
            select(MemoryRecordV2).where(
                MemoryRecordV2.owner_id == self.owner_id,
                MemoryRecordV2.id == identifier,
                MemoryRecordV2.status.in_(values),
            )
        )

    def eligible_records_statement(
        self,
        *,
        now: datetime,
        memory_types: Collection[MemoryType | str] = (),
        domain_keys: Collection[str] = (),
    ) -> Select[tuple[MemoryRecordV2]]:
        """Build the authoritative normal-serving query with SQL-level eligibility."""
        statement = select(MemoryRecordV2).where(
            MemoryRecordV2.owner_id == self.owner_id,
            MemoryRecordV2.status == MemoryLifecycleState.ACTIVE.value,
            or_(MemoryRecordV2.expires_at.is_(None), MemoryRecordV2.expires_at > now),
        )
        if memory_types:
            values = tuple(
                item.value if isinstance(item, MemoryType) else MemoryType(item).value
                for item in memory_types
            )
            statement = statement.where(MemoryRecordV2.memory_type.in_(values))
        if domain_keys:
            statement = statement.where(MemoryRecordV2.domain_key.in_(tuple(domain_keys)))
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
        conditions = [MemoryRecordV2.owner_id == self.owner_id]
        if memory_id is not None:
            conditions.append(MemoryRecordV2.id == canonical_uuid(memory_id))
        if memory_types:
            values = tuple(
                item.value if isinstance(item, MemoryType) else MemoryType(item).value
                for item in memory_types
            )
            conditions.append(MemoryRecordV2.memory_type.in_(values))
        if domain_keys:
            conditions.append(MemoryRecordV2.domain_key.in_(tuple(domain_keys)))
        normalized_slots = tuple(item.strip() for item in slot_keys if item.strip())
        if normalized_slots:
            conditions.append(MemoryRecordV2.slot_key.in_(normalized_slots))
        inactive = self._session.scalar(
            select(func.count()).select_from(MemoryRecordV2).where(
                *conditions,
                MemoryRecordV2.status != MemoryLifecycleState.ACTIVE.value,
            )
        )
        expired = self._session.scalar(
            select(func.count()).select_from(MemoryRecordV2).where(
                *conditions,
                MemoryRecordV2.status == MemoryLifecycleState.ACTIVE.value,
                MemoryRecordV2.expires_at.is_not(None),
                MemoryRecordV2.expires_at <= now,
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
    ) -> list[MemoryRecordV2]:
        if not 1 <= limit <= 500:
            raise ValueError("recall_candidate_limit_out_of_range")
        statement = self.eligible_records_statement(
            now=now,
            memory_types=memory_types,
            domain_keys=domain_keys,
        ).order_by(
            MemoryRecordV2.last_confirmed_at.desc(),
            MemoryRecordV2.updated_at.desc(),
            MemoryRecordV2.id.asc(),
        )
        return list(self._session.scalars(statement.limit(limit)))

    def get_recall_eligible_by_id(
        self,
        memory_id: str,
        *,
        now: datetime,
    ) -> MemoryRecordV2 | None:
        identifier = canonical_uuid(memory_id)
        return self._session.scalar(
            self.eligible_records_statement(now=now).where(MemoryRecordV2.id == identifier)
        )

    def find_recall_eligible_slot(
        self,
        *,
        now: datetime,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
    ) -> MemoryRecordV2 | None:
        statement = self.eligible_records_statement(
            now=now,
            memory_types=(memory_type,),
            domain_keys=(domain_key,),
        ).where(MemoryRecordV2.slot_key == slot_key)
        return self._session.scalar(
            statement.order_by(
                MemoryRecordV2.last_confirmed_at.desc(),
                MemoryRecordV2.id.asc(),
            ).limit(1)
        )

    def list_recall_eligible_for_slots(
        self,
        slot_keys: Collection[str],
        *,
        now: datetime,
        limit: int = 100,
    ) -> list[MemoryRecordV2]:
        slots = tuple(dict.fromkeys(item.strip() for item in slot_keys if item.strip()))
        if not slots:
            return []
        if len(slots) > 50 or not 1 <= limit <= 100:
            raise ValueError("trusted_slot_query_out_of_range")
        statement = self.eligible_records_statement(now=now).where(
            MemoryRecordV2.slot_key.in_(slots)
        )
        return list(
            self._session.scalars(
                statement.order_by(
                    MemoryRecordV2.last_confirmed_at.desc(),
                    MemoryRecordV2.id.asc(),
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
            select(MemorySourceV2.memory_id, MemorySourceV2.id).where(
                MemorySourceV2.owner_id == self.owner_id,
                MemorySourceV2.memory_id.in_(identifiers),
                MemorySourceV2.is_active.is_(True),
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
    ) -> tuple[str, ...]:
        identifiers = tuple(dict.fromkeys(canonical_uuid(item) for item in memory_ids))
        if not identifiers:
            return ()
        with self._session.begin_nested() as savepoint:
            result = self._session.execute(
                update(MemoryRecordV2)
                .where(
                    MemoryRecordV2.owner_id == self.owner_id,
                    MemoryRecordV2.id.in_(identifiers),
                    MemoryRecordV2.status == MemoryLifecycleState.ACTIVE.value,
                    or_(
                        MemoryRecordV2.expires_at.is_(None),
                        MemoryRecordV2.expires_at > used_at,
                    ),
                )
                .values(
                    usage_count=MemoryRecordV2.usage_count + 1,
                    last_used_at=used_at,
                )
            )
            if result.rowcount != len(identifiers):
                savepoint.rollback()
                raise MemoryV2NotFoundError("usage_selection_not_fully_eligible")
        self._session.flush()
        return identifiers

    def list_records(
        self,
        *,
        statuses: Collection[MemoryLifecycleState | str],
        memory_type: MemoryType | None = None,
        limit: int = 100,
    ) -> list[MemoryRecordV2]:
        if not 1 <= limit <= 1_000:
            raise ValueError("record_limit_out_of_range")
        values = self._validated_statuses(statuses)
        statement: Select[tuple[MemoryRecordV2]] = select(MemoryRecordV2).where(
            MemoryRecordV2.owner_id == self.owner_id,
            MemoryRecordV2.status.in_(values),
        )
        if memory_type is not None:
            statement = statement.where(MemoryRecordV2.memory_type == memory_type.value)
        statement = statement.order_by(MemoryRecordV2.created_at.desc()).limit(limit)
        return list(self._session.scalars(statement))

    def find_active_slot(
        self,
        *,
        subject_key: str,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
    ) -> list[MemoryRecordV2]:
        statement = select(MemoryRecordV2).where(
            MemoryRecordV2.owner_id == self.owner_id,
            MemoryRecordV2.status == MemoryLifecycleState.ACTIVE.value,
            MemoryRecordV2.subject_key == subject_key,
            MemoryRecordV2.memory_type == memory_type.value,
            MemoryRecordV2.domain_key == domain_key,
            MemoryRecordV2.slot_key == slot_key,
        )
        return list(self._session.scalars(statement))

    def find_active_fingerprint(self, fingerprint: str) -> MemoryRecordV2 | None:
        return self._session.scalar(
            select(MemoryRecordV2).where(
                MemoryRecordV2.owner_id == self.owner_id,
                MemoryRecordV2.status == MemoryLifecycleState.ACTIVE.value,
                MemoryRecordV2.canonical_fingerprint == fingerprint,
            )
        )

    def get_operation_by_idempotency_key(self, key: str) -> MemoryOperationV2 | None:
        return self._session.scalar(
            select(MemoryOperationV2).where(
                MemoryOperationV2.owner_id == self.owner_id,
                MemoryOperationV2.idempotency_key == key,
            )
        )

    def add_operation(self, operation: MemoryOperationV2) -> MemoryOperationV2:
        self._require_owned(operation)
        self._reject_prohibited_material(operation.normalized_command_json, operation.error_detail)
        self._session.add(operation)
        self._session.flush()
        return operation

    def add_record(self, record: MemoryRecordV2) -> MemoryRecordV2:
        self._require_owned(record)
        self._reject_prohibited_material(
            record.canonical_payload,
            record.display_text,
            record.metadata_json,
        )
        if not self._operation_exists(record.created_by_operation_id):
            raise MemoryV2NotFoundError("creating_operation_not_found")
        unknown_metadata = set(record.metadata_json or {}) - ALLOWED_RECORD_METADATA_KEYS
        if unknown_metadata:
            raise ValueError(
                f"record_metadata_keys_not_allowed:{','.join(sorted(unknown_metadata))}"
            )
        self._session.add(record)
        self._session.flush()
        return record

    def add_candidate(self, candidate: MemoryCandidateV2) -> MemoryCandidateV2:
        self._require_owned(candidate)
        self._reject_prohibited_material(
            candidate.canonical_payload,
            candidate.display_text,
            candidate.decision_reason,
        )
        for target_id in candidate.trusted_target_ids or []:
            if not self._record_exists(target_id):
                raise MemoryV2NotFoundError("candidate_target_not_found")
        self._session.add(candidate)
        self._session.flush()
        return candidate

    def get_candidate(self, candidate_id: str) -> MemoryCandidateV2 | None:
        identifier = canonical_uuid(candidate_id)
        return self._session.scalar(
            select(MemoryCandidateV2).where(
                MemoryCandidateV2.owner_id == self.owner_id,
                MemoryCandidateV2.id == identifier,
            )
        )

    def update_candidate_decision(
        self,
        candidate_id: str,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> MemoryCandidateV2:
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
            raise MemoryV2NotFoundError("candidate_operation_not_found")
        self._reject_prohibited_material(values.get("decision_reason"))
        result = self._session.execute(
            update(MemoryCandidateV2)
            .where(
                MemoryCandidateV2.owner_id == self.owner_id,
                MemoryCandidateV2.id == identifier,
                MemoryCandidateV2.revision == expected_revision,
            )
            .values(
                **values,
                revision=MemoryCandidateV2.revision + 1,
                updated_at=func.now(),
            )
        )
        if result.rowcount != 1:
            raise MemoryV2RevisionConflict("candidate_revision_conflict_or_not_found")
        self._session.flush()
        candidate = self.get_candidate(identifier)
        if candidate is None:
            raise MemoryV2NotFoundError("candidate_not_found")
        return candidate

    def add_source(self, source: MemorySourceV2) -> MemorySourceV2:
        self._require_owned(source)
        self._reject_prohibited_material(source.redacted_excerpt)
        if not self._record_exists(source.memory_id) or not self._operation_exists(
            source.operation_id
        ):
            raise MemoryV2NotFoundError("source_reference_not_found")
        self._session.add(source)
        self._session.flush()
        return source

    def add_relation(self, relation: MemoryRelationV2) -> MemoryRelationV2:
        self._require_owned(relation)
        if not self._record_exists(relation.from_memory_id) or not self._record_exists(
            relation.to_memory_id
        ):
            raise MemoryV2NotFoundError("relation_endpoint_not_found")
        if not self._operation_exists(relation.operation_id):
            raise MemoryV2NotFoundError("relation_operation_not_found")
        self._session.add(relation)
        self._session.flush()
        return relation

    def add_outbox_event(self, event: MemoryOutboxV2) -> MemoryOutboxV2:
        self._require_owned(event)
        self._reject_prohibited_material(event.event_payload_json, event.last_error)
        if event.memory_id is not None and not self._record_exists(event.memory_id):
            raise MemoryV2NotFoundError("outbox_memory_not_found")
        self._session.add(event)
        self._session.flush()
        return event

    def add_tombstone(self, tombstone: MemoryTombstoneV2) -> MemoryTombstoneV2:
        self._require_owned(tombstone)
        if not self._operation_exists(tombstone.originating_operation_id):
            raise MemoryV2NotFoundError("tombstone_operation_not_found")
        self._session.add(tombstone)
        self._session.flush()
        return tombstone

    def add_migration_run(self, run: MemoryMigrationRunV2) -> MemoryMigrationRunV2:
        self._session.add(self._require_owned(run))
        self._session.flush()
        return run

    def add_legacy_map(self, mapping: MemoryLegacyMapV2) -> MemoryLegacyMapV2:
        self._require_owned(mapping)
        if mapping.memory_id is not None and not self._record_exists(mapping.memory_id):
            raise MemoryV2NotFoundError("legacy_map_memory_not_found")
        if mapping.migration_run_id is not None and not self._migration_run_exists(
            mapping.migration_run_id
        ):
            raise MemoryV2NotFoundError("legacy_map_run_not_found")
        self._session.add(mapping)
        self._session.flush()
        return mapping

    def update_record_fields(
        self,
        memory_id: str,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> MemoryRecordV2:
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
            update(MemoryRecordV2)
            .where(
                MemoryRecordV2.owner_id == self.owner_id,
                MemoryRecordV2.id == identifier,
                MemoryRecordV2.revision == expected_revision,
            )
            .values(
                **values,
                revision=MemoryRecordV2.revision + 1,
                updated_at=func.now(),
            )
        )
        result = self._session.execute(statement)
        if result.rowcount != 1:
            raise MemoryV2RevisionConflict("record_revision_conflict_or_not_found")
        self._session.flush()
        record = self._session.scalar(
            select(MemoryRecordV2).where(
                MemoryRecordV2.owner_id == self.owner_id,
                MemoryRecordV2.id == identifier,
            )
        )
        if record is None:
            raise MemoryV2NotFoundError("record_not_found")
        return record

    def delete_tombstone(self, tombstone_id: str) -> bool:
        identifier = canonical_uuid(tombstone_id)
        tombstone = self._session.scalar(
            select(MemoryTombstoneV2).where(
                MemoryTombstoneV2.owner_id == self.owner_id,
                MemoryTombstoneV2.id == identifier,
            )
        )
        if tombstone is None:
            return False
        self._session.delete(tombstone)
        self._session.flush()
        return True
