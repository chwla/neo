"""Pure deterministic planner for memory-v2 canonical state transitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue

from app.services.memory_v2.contracts import (
    ArchiveMemoryCommand,
    CandidateLifecycleState,
    CreateMemoryCommand,
    ErasePermanentlyMemoryCommand,
    ForgetMemoryCommand,
    MemoryCommand,
    MemoryErrorCode,
    MemoryLifecycleState,
    MemoryOperationKind,
    MemoryOutcome,
    MemoryRejectionCode,
    MergeMemoryCommand,
    ReplaceMemoryCommand,
    RestoreMemoryCommand,
    RestoreMode,
    SupersedeMemoryCommand,
    UpdateMemoryCommand,
)
from app.services.memory_v2.crypto import KeyedDigest
from app.services.memory_v2.normalization import (
    NormalizedCandidate,
    NormalizedRecordValue,
    NormalizedSource,
    compatible_refinement,
)
from app.services.memory_v2.taxonomy import Cardinality, MemoryIdentity, MemoryType
from app.services.memory_v2.tombstones import TombstoneSnapshot, tombstone_expiration


@dataclass(frozen=True)
class RecordSnapshot:
    id: str
    owner_id: str
    subject_key: str
    memory_type: MemoryType
    domain_key: str
    slot_key: str
    cardinality: Cardinality
    sensitivity: str
    canonical_value: JsonValue | None
    display_text: str | None
    canonical_fingerprint: str
    confidence: float
    importance: int
    status: MemoryLifecycleState
    revision: int
    usage_count: int
    pinned: bool
    metadata: dict[str, Any]
    value_schema_version: int

    @property
    def identity(self) -> MemoryIdentity:
        return MemoryIdentity(
            memory_type=self.memory_type,
            domain_key=self.domain_key,
            slot_key=self.slot_key,
            cardinality=self.cardinality,
        )


@dataclass(frozen=True)
class RelationSnapshot:
    from_memory_id: str
    relation_type: str
    to_memory_id: str


@dataclass(frozen=True)
class CandidateSnapshot:
    id: str
    state: CandidateLifecycleState


@dataclass(frozen=True)
class PlannerState:
    owner_id: str
    records: tuple[RecordSnapshot, ...]
    candidates: tuple[CandidateSnapshot, ...] = ()
    relations: tuple[RelationSnapshot, ...] = ()
    tombstones: tuple[TombstoneSnapshot, ...] = ()

    def record(self, memory_id: str) -> RecordSnapshot | None:
        return next((item for item in self.records if item.id == memory_id), None)

    def active_slot(
        self,
        *,
        subject_key: str,
        memory_type: MemoryType,
        domain_key: str,
        slot_key: str,
    ) -> tuple[RecordSnapshot, ...]:
        return tuple(
            item
            for item in self.records
            if item.status is MemoryLifecycleState.ACTIVE
            and item.subject_key == subject_key
            and item.memory_type is memory_type
            and item.domain_key == domain_key
            and item.slot_key == slot_key
        )


@dataclass(frozen=True)
class RecordCreateSpec:
    id: str
    candidate: NormalizedCandidate
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordUpdateSpec:
    id: str
    expected_revision: int
    values: tuple[tuple[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class RecordDeleteSpec:
    id: str
    expected_revision: int


@dataclass(frozen=True)
class CandidateDecisionSpec:
    candidate: NormalizedCandidate
    state: CandidateLifecycleState
    outcome: MemoryOutcome
    rejection_code: MemoryRejectionCode | None = None
    reason: str | None = None
    applied_memory_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationCreateSpec:
    id: str
    from_memory_id: str
    relation_type: str
    to_memory_id: str


@dataclass(frozen=True)
class SourceCreateSpec:
    id: str
    memory_id: str
    assertion_role: str
    source: NormalizedSource
    evidence_index: int | None
    structured_evidence: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SourceDetachSpec:
    memory_id: str
    reason: str


@dataclass(frozen=True)
class TombstoneCreateSpec:
    id: str
    memory_id: str
    fingerprint: KeyedDigest
    memory_type: MemoryType
    domain_key: str
    slot_key: str
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class TombstoneReconfirmSpec:
    id: str
    reconfirmed_at: datetime


@dataclass(frozen=True)
class OutboxIntent:
    id: str
    event_kind: str
    memory_id: str | None
    canonical_revision: int | None
    content_hash: str | None
    event_payload: tuple[tuple[str, Any], ...]
    idempotency_key: str


@dataclass(frozen=True)
class RecordPrecondition:
    id: str
    expected_revision: int
    expected_status: MemoryLifecycleState


@dataclass(frozen=True)
class PlannerContext:
    owner_id: str
    operation_id: str
    now: datetime
    source: NormalizedSource
    normalized_candidate: NormalizedCandidate | None = None
    normalized_update: NormalizedRecordValue | None = None
    blocking_tombstone_id: str | None = None
    reconfirm_tombstone_ids: tuple[str, ...] = ()
    forget_fingerprints: tuple[tuple[str, KeyedDigest], ...] = ()

    def forget_fingerprint(self, memory_id: str) -> KeyedDigest | None:
        return dict(self.forget_fingerprints).get(memory_id)


@dataclass(frozen=True)
class MutationPlan:
    operation_id: str
    operation: MemoryOperationKind
    outcome: MemoryOutcome
    operation_status: str
    affected_memory_ids: tuple[str, ...] = ()
    active_memory_ids: tuple[str, ...] = ()
    current_revision: int | None = None
    rejection_code: MemoryRejectionCode | None = None
    error_code: MemoryErrorCode | None = None
    records_to_create: tuple[RecordCreateSpec, ...] = ()
    records_to_update: tuple[RecordUpdateSpec, ...] = ()
    records_to_delete: tuple[RecordDeleteSpec, ...] = ()
    candidate_decisions: tuple[CandidateDecisionSpec, ...] = ()
    relations_to_create: tuple[RelationCreateSpec, ...] = ()
    sources_to_create: tuple[SourceCreateSpec, ...] = ()
    sources_to_detach: tuple[SourceDetachSpec, ...] = ()
    tombstones_to_create: tuple[TombstoneCreateSpec, ...] = ()
    tombstones_to_reconfirm: tuple[TombstoneReconfirmSpec, ...] = ()
    tombstones_to_delete: tuple[str, ...] = ()
    outbox_events: tuple[OutboxIntent, ...] = ()
    preconditions: tuple[RecordPrecondition, ...] = ()
    message: str | None = None

    @property
    def mutates_canonical_state(self) -> bool:
        return bool(
            self.records_to_create
            or self.records_to_update
            or self.records_to_delete
            or self.relations_to_create
            or self.sources_to_create
            or self.tombstones_to_create
            or self.tombstones_to_reconfirm
            or self.tombstones_to_delete
            or self.outbox_events
        )


def _plan_uuid(operation_id: str, label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"neo.memory.v2:{operation_id}:{label}"))


def _precondition(record: RecordSnapshot) -> RecordPrecondition:
    return RecordPrecondition(record.id, record.revision, record.status)


def _update(record: RecordSnapshot, **values: Any) -> RecordUpdateSpec:
    return RecordUpdateSpec(
        id=record.id,
        expected_revision=record.revision,
        values=tuple(sorted(values.items())),
    )


def _outbox(
    context: PlannerContext,
    *,
    label: str,
    event_kind: str,
    memory_id: str | None,
    revision: int | None,
    content_hash: str | None,
    payload: dict[str, Any] | None = None,
) -> OutboxIntent:
    owner_key = "none" if memory_id is None else memory_id
    revision_key = "none" if revision is None else str(revision)
    return OutboxIntent(
        id=_plan_uuid(context.operation_id, f"outbox:{label}"),
        event_kind=event_kind,
        memory_id=memory_id,
        canonical_revision=revision,
        content_hash=content_hash,
        event_payload=tuple(sorted((payload or {}).items())),
        idempotency_key=(
            f"memory-v2:{context.owner_id}:{event_kind}:{owner_key}:"
            f"{revision_key}:{context.operation_id}:{label}"
        ),
    )


def _candidate_decision(
    candidate: NormalizedCandidate,
    *,
    state: CandidateLifecycleState,
    outcome: MemoryOutcome,
    rejection_code: MemoryRejectionCode | None = None,
    reason: str | None = None,
    memory_ids: tuple[str, ...] = (),
) -> CandidateDecisionSpec:
    return CandidateDecisionSpec(
        candidate=candidate,
        state=state,
        outcome=outcome,
        rejection_code=rejection_code,
        reason=reason,
        applied_memory_ids=memory_ids,
    )


def _rejected(
    command: MemoryCommand,
    context: PlannerContext,
    *,
    outcome: MemoryOutcome = MemoryOutcome.REJECTED,
    rejection_code: MemoryRejectionCode | None = None,
    error_code: MemoryErrorCode | None = None,
    message: str,
    candidate_state: CandidateLifecycleState = CandidateLifecycleState.REJECTED,
) -> MutationPlan:
    decisions: tuple[CandidateDecisionSpec, ...] = ()
    if context.normalized_candidate is not None:
        decisions = (
            _candidate_decision(
                context.normalized_candidate,
                state=candidate_state,
                outcome=outcome,
                rejection_code=rejection_code,
                reason=message,
            ),
        )
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=outcome,
        operation_status="failed" if error_code else "rejected",
        rejection_code=rejection_code,
        error_code=error_code,
        candidate_decisions=decisions,
        message=message,
    )


def _source_specs(
    context: PlannerContext,
    *,
    memory_id: str,
    role: str,
    evidence_role: str,
    label: str,
    structured: dict[str, str] | None = None,
) -> tuple[SourceCreateSpec, ...]:
    evidence = tuple(
        item
        for item in (
            *context.source.evidence,
            *(context.normalized_candidate.evidence if context.normalized_candidate else ()),
        )
        if item.role.value == evidence_role
    )
    if not evidence:
        return (
            SourceCreateSpec(
                id=_plan_uuid(context.operation_id, f"source:{label}:structured"),
                memory_id=memory_id,
                assertion_role=role,
                source=context.source,
                evidence_index=None,
                structured_evidence=tuple(sorted((structured or {}).items())),
            ),
        )
    return tuple(
        SourceCreateSpec(
            id=_plan_uuid(context.operation_id, f"source:{label}:{index}"),
            memory_id=memory_id,
            assertion_role=role,
            source=context.source,
            evidence_index=index,
            structured_evidence=tuple(sorted((structured or {}).items())),
        )
        for index, _item in enumerate(evidence)
    )


def _required_record(
    command: MemoryCommand,
    context: PlannerContext,
    state: PlannerState,
    *,
    memory_id: str,
    expected_revision: int,
) -> RecordSnapshot | MutationPlan:
    record = state.record(memory_id)
    if record is None:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.NOT_FOUND,
            message="memory_not_found",
        )
    if record.revision != expected_revision:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.REVISION_CONFLICT,
            message="revision_conflict",
        )
    return record


def _plan_create(
    command: CreateMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    candidate = context.normalized_candidate
    if candidate is None:
        raise ValueError("normalized_candidate_required")
    if context.blocking_tombstone_id and not candidate.explicit_user_request:
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.RESURRECTION_BLOCKED,
            message="automatic_resurrection_blocked",
        )

    duplicate = next(
        (
            item
            for item in state.records
            if item.status is MemoryLifecycleState.ACTIVE
            and item.canonical_fingerprint == candidate.canonical_fingerprint
        ),
        None,
    )
    if duplicate is not None:
        revision = duplicate.revision + 1
        return MutationPlan(
            operation_id=context.operation_id,
            operation=command.operation,
            outcome=MemoryOutcome.RECONFIRMED,
            operation_status="committed",
            affected_memory_ids=(duplicate.id,),
            active_memory_ids=(duplicate.id,),
            current_revision=revision,
            records_to_update=(_update(duplicate, last_confirmed_at=context.now),),
            candidate_decisions=(
                _candidate_decision(
                    candidate,
                    state=CandidateLifecycleState.APPLIED,
                    outcome=MemoryOutcome.RECONFIRMED,
                    memory_ids=(duplicate.id,),
                ),
            ),
            sources_to_create=_source_specs(
                context,
                memory_id=duplicate.id,
                role="supports",
                evidence_role="assertion",
                label="reconfirm",
            ),
            tombstones_to_reconfirm=tuple(
                TombstoneReconfirmSpec(item, context.now)
                for item in context.reconfirm_tombstone_ids
            ),
            outbox_events=(
                _outbox(
                    context,
                    label="reconfirm",
                    event_kind="canonical_upsert",
                    memory_id=duplicate.id,
                    revision=revision,
                    content_hash=duplicate.canonical_fingerprint,
                ),
            ),
            preconditions=(_precondition(duplicate),),
        )

    occupied = state.active_slot(
        subject_key=candidate.subject_key,
        memory_type=candidate.memory_type,
        domain_key=candidate.domain_key,
        slot_key=candidate.slot_key,
    )
    if candidate.cardinality is Cardinality.EXCLUSIVE and occupied:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.NEEDS_REVIEW,
            rejection_code=MemoryRejectionCode.AMBIGUOUS_CONFLICT,
            message="occupied_exclusive_slot_requires_replace",
            candidate_state=CandidateLifecycleState.NEEDS_REVIEW,
        )

    memory_id = _plan_uuid(context.operation_id, "record:create")
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.CREATED,
        operation_status="committed",
        affected_memory_ids=(memory_id,),
        active_memory_ids=(memory_id,),
        current_revision=1,
        records_to_create=(RecordCreateSpec(memory_id, candidate),),
        candidate_decisions=(
            _candidate_decision(
                candidate,
                state=CandidateLifecycleState.APPLIED,
                outcome=MemoryOutcome.CREATED,
                memory_ids=(memory_id,),
            ),
        ),
        sources_to_create=_source_specs(
            context,
            memory_id=memory_id,
            role="supports",
            evidence_role="assertion",
            label="create",
        ),
        tombstones_to_reconfirm=tuple(
            TombstoneReconfirmSpec(item, context.now) for item in context.reconfirm_tombstone_ids
        ),
        outbox_events=(
            _outbox(
                context,
                label="create",
                event_kind="canonical_upsert",
                memory_id=memory_id,
                revision=1,
                content_hash=candidate.canonical_fingerprint,
            ),
        ),
    )


def _plan_update(
    command: UpdateMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    required = _required_record(
        command,
        context,
        state,
        memory_id=str(command.target.memory_id),
        expected_revision=command.target.expected_revision,
    )
    if isinstance(required, MutationPlan):
        return required
    if required.status is not MemoryLifecycleState.ACTIVE:
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE,
            message="update_requires_active_target",
        )

    normalized = context.normalized_update
    values: dict[str, Any] = {}
    if normalized is not None:
        if required.canonical_value is None:
            compatible = normalized.canonical_fingerprint == required.canonical_fingerprint
        else:
            compatible = compatible_refinement(
                required.canonical_value,
                normalized.canonical_value,
            )
        if not compatible:
            return _rejected(
                command,
                context,
                rejection_code=MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE,
                message="incompatible_update_requires_replace",
            )
        values.update(
            canonical_payload=normalized.canonical_value,
            display_text=normalized.display_text,
            sensitivity=normalized.sensitivity.value,
            canonical_fingerprint=normalized.canonical_fingerprint,
            last_confirmed_at=context.now,
        )
    for field_name in ("confidence", "importance", "pinned", "expires_at"):
        value = getattr(command.patch, field_name)
        if value is not None:
            values[field_name] = value
    if not values:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.INVALID_COMMAND,
            message="update_has_no_material_change",
        )
    revision = required.revision + 1
    fingerprint = normalized.canonical_fingerprint if normalized else required.canonical_fingerprint
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.REFINED,
        operation_status="committed",
        affected_memory_ids=(required.id,),
        active_memory_ids=(required.id,),
        current_revision=revision,
        records_to_update=(_update(required, **values),),
        outbox_events=(
            _outbox(
                context,
                label="update",
                event_kind="canonical_upsert",
                memory_id=required.id,
                revision=revision,
                content_hash=fingerprint,
            ),
        ),
        preconditions=(_precondition(required),),
    )


def _replacement_plan(
    command: ReplaceMemoryCommand | RestoreMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
    predecessors: tuple[RecordSnapshot, ...],
    *,
    outcome: MemoryOutcome = MemoryOutcome.REPLACED,
) -> MutationPlan:
    candidate = context.normalized_candidate
    if candidate is None:
        raise ValueError("normalized_candidate_required")
    if not predecessors:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.NOT_FOUND,
            message="replacement_target_not_found",
        )
    if any(item.status is not MemoryLifecycleState.ACTIVE for item in predecessors):
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.REPLACEMENT_TARGET_NOT_FOUND,
            message="replacement_target_not_active",
        )

    targeted = {item.id for item in predecessors}
    occupied = state.active_slot(
        subject_key=candidate.subject_key,
        memory_type=candidate.memory_type,
        domain_key=candidate.domain_key,
        slot_key=candidate.slot_key,
    )
    untargeted = tuple(item for item in occupied if item.id not in targeted)
    if candidate.cardinality is Cardinality.EXCLUSIVE and untargeted:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.NEEDS_REVIEW,
            rejection_code=MemoryRejectionCode.AMBIGUOUS_CONFLICT,
            message="replacement_slot_has_untargeted_active_record",
            candidate_state=CandidateLifecycleState.NEEDS_REVIEW,
        )

    memory_id = _plan_uuid(context.operation_id, "record:replacement")
    updates = tuple(
        _update(item, status=MemoryLifecycleState.SUPERSEDED.value) for item in predecessors
    )
    relations = tuple(
        RelationCreateSpec(
            id=_plan_uuid(context.operation_id, f"relation:supersedes:{item.id}"),
            from_memory_id=memory_id,
            relation_type="supersedes",
            to_memory_id=item.id,
        )
        for item in predecessors
    )
    sources = list(
        _source_specs(
            context,
            memory_id=memory_id,
            role="supports",
            evidence_role="assertion",
            label="replacement:assertion",
            structured={"operation": "replace"},
        )
    )
    for predecessor in predecessors:
        sources.extend(
            _source_specs(
                context,
                memory_id=predecessor.id,
                role="retracts_predecessor",
                evidence_role="retraction",
                label=f"replacement:retraction:{predecessor.id}",
                structured={"operation": "replace", "target_id": predecessor.id},
            )
        )
    outbox = [
        _outbox(
            context,
            label=f"remove:{item.id}",
            event_kind="canonical_remove",
            memory_id=item.id,
            revision=item.revision + 1,
            content_hash=item.canonical_fingerprint,
        )
        for item in predecessors
    ]
    outbox.append(
        _outbox(
            context,
            label="replacement:upsert",
            event_kind="canonical_upsert",
            memory_id=memory_id,
            revision=1,
            content_hash=candidate.canonical_fingerprint,
        )
    )
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=outcome,
        operation_status="committed",
        affected_memory_ids=(memory_id, *(item.id for item in predecessors)),
        active_memory_ids=(memory_id,),
        current_revision=1,
        records_to_create=(RecordCreateSpec(memory_id, candidate),),
        records_to_update=updates,
        candidate_decisions=(
            _candidate_decision(
                candidate,
                state=CandidateLifecycleState.APPLIED,
                outcome=outcome,
                memory_ids=(memory_id,),
            ),
        ),
        relations_to_create=relations,
        sources_to_create=tuple(sources),
        tombstones_to_reconfirm=tuple(
            TombstoneReconfirmSpec(item, context.now) for item in context.reconfirm_tombstone_ids
        ),
        outbox_events=tuple(outbox),
        preconditions=tuple(_precondition(item) for item in predecessors),
    )


def _plan_replace(
    command: ReplaceMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    targets = command.targets
    predecessors: list[RecordSnapshot] = []
    if targets:
        for target in targets:
            required = _required_record(
                command,
                context,
                state,
                memory_id=str(target.memory_id),
                expected_revision=target.expected_revision,
            )
            if isinstance(required, MutationPlan):
                return required
            predecessors.append(required)
    elif context.normalized_candidate is not None:
        candidate = context.normalized_candidate
        matches = state.active_slot(
            subject_key=candidate.subject_key,
            memory_type=candidate.memory_type,
            domain_key=candidate.domain_key,
            slot_key=candidate.slot_key,
        )
        if len(matches) != 1:
            return _rejected(
                command,
                context,
                rejection_code=MemoryRejectionCode.REPLACEMENT_TARGET_NOT_FOUND,
                message="grounded_same_slot_target_not_deterministic",
            )
        predecessors.extend(matches)
    return _replacement_plan(command, state, context, tuple(predecessors))


def _plan_supersede(
    command: SupersedeMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    successor = state.record(str(command.successor_memory_id))
    if successor is None or successor.status is not MemoryLifecycleState.ACTIVE:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.NOT_FOUND,
            message="active_successor_not_found",
        )
    predecessors: list[RecordSnapshot] = []
    for target in command.predecessors:
        required = _required_record(
            command,
            context,
            state,
            memory_id=str(target.memory_id),
            expected_revision=target.expected_revision,
        )
        if isinstance(required, MutationPlan):
            return required
        if required.id == successor.id:
            return _rejected(
                command,
                context,
                outcome=MemoryOutcome.FAILED,
                error_code=MemoryErrorCode.INVALID_COMMAND,
                message="successor_cannot_supersede_itself",
            )
        predecessors.append(required)
    if any(item.status is not MemoryLifecycleState.ACTIVE for item in predecessors):
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.REPLACEMENT_TARGET_NOT_FOUND,
            message="supersede_predecessor_not_active",
        )
    relations = tuple(
        RelationCreateSpec(
            _plan_uuid(context.operation_id, f"relation:supersede:{item.id}"),
            successor.id,
            "supersedes",
            item.id,
        )
        for item in predecessors
    )
    outbox = tuple(
        _outbox(
            context,
            label=f"supersede:{item.id}",
            event_kind="canonical_remove",
            memory_id=item.id,
            revision=item.revision + 1,
            content_hash=item.canonical_fingerprint,
        )
        for item in predecessors
    )
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.SUPERSEDED,
        operation_status="committed",
        affected_memory_ids=(successor.id, *(item.id for item in predecessors)),
        active_memory_ids=(successor.id,),
        current_revision=successor.revision,
        records_to_update=tuple(
            _update(item, status=MemoryLifecycleState.SUPERSEDED.value) for item in predecessors
        ),
        relations_to_create=relations,
        outbox_events=outbox,
        preconditions=(_precondition(successor), *(_precondition(item) for item in predecessors)),
    )


def _plan_merge(
    command: MergeMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    candidate = context.normalized_candidate
    if candidate is None:
        raise ValueError("normalized_candidate_required")
    sources: list[RecordSnapshot] = []
    for target in command.sources:
        required = _required_record(
            command,
            context,
            state,
            memory_id=str(target.memory_id),
            expected_revision=target.expected_revision,
        )
        if isinstance(required, MutationPlan):
            return required
        sources.append(required)
    if any(item.status is not MemoryLifecycleState.ACTIVE for item in sources):
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE,
            message="merge_requires_active_sources",
        )
    if any(
        item.memory_type is not candidate.memory_type
        or item.domain_key != candidate.domain_key
        or item.cardinality is not candidate.cardinality
        for item in sources
    ):
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE,
            message="semantically_incompatible_merge",
        )
    memory_id = _plan_uuid(context.operation_id, "record:merge")
    relations = tuple(
        RelationCreateSpec(
            _plan_uuid(context.operation_id, f"relation:merged:{item.id}"),
            memory_id,
            "merged_from",
            item.id,
        )
        for item in sources
    )
    outbox = [
        _outbox(
            context,
            label=f"merge:remove:{item.id}",
            event_kind="canonical_remove",
            memory_id=item.id,
            revision=item.revision + 1,
            content_hash=item.canonical_fingerprint,
        )
        for item in sources
    ]
    outbox.append(
        _outbox(
            context,
            label="merge:upsert",
            event_kind="canonical_upsert",
            memory_id=memory_id,
            revision=1,
            content_hash=candidate.canonical_fingerprint,
        )
    )
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.MERGED,
        operation_status="committed",
        affected_memory_ids=(memory_id, *(item.id for item in sources)),
        active_memory_ids=(memory_id,),
        current_revision=1,
        records_to_create=(RecordCreateSpec(memory_id, candidate),),
        records_to_update=tuple(
            _update(item, status=MemoryLifecycleState.SUPERSEDED.value) for item in sources
        ),
        candidate_decisions=(
            _candidate_decision(
                candidate,
                state=CandidateLifecycleState.APPLIED,
                outcome=MemoryOutcome.MERGED,
                memory_ids=(memory_id,),
            ),
        ),
        relations_to_create=relations,
        sources_to_create=_source_specs(
            context,
            memory_id=memory_id,
            role="supports",
            evidence_role="assertion",
            label="merge",
            structured={"operation": "merge"},
        ),
        outbox_events=tuple(outbox),
        preconditions=tuple(_precondition(item) for item in sources),
    )


def _plan_archive(
    command: ArchiveMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    required = _required_record(
        command,
        context,
        state,
        memory_id=str(command.target.memory_id),
        expected_revision=command.target.expected_revision,
    )
    if isinstance(required, MutationPlan):
        return required
    if required.status is not MemoryLifecycleState.ACTIVE:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.INVALID_COMMAND,
            message="archive_requires_active_target",
        )
    revision = required.revision + 1
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.ARCHIVED,
        operation_status="committed",
        affected_memory_ids=(required.id,),
        current_revision=revision,
        records_to_update=(_update(required, status=MemoryLifecycleState.ARCHIVED.value),),
        outbox_events=(
            _outbox(
                context,
                label="archive",
                event_kind="canonical_remove",
                memory_id=required.id,
                revision=revision,
                content_hash=required.canonical_fingerprint,
            ),
        ),
        preconditions=(_precondition(required),),
    )


def _plan_forget(
    command: ForgetMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    required = _required_record(
        command,
        context,
        state,
        memory_id=str(command.target.memory_id),
        expected_revision=command.target.expected_revision,
    )
    if isinstance(required, MutationPlan):
        return required
    if required.status is MemoryLifecycleState.FORGOTTEN:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.INVALID_COMMAND,
            message="target_already_forgotten",
        )
    fingerprint = context.forget_fingerprint(required.id)
    if fingerprint is None:
        raise ValueError("forget_fingerprint_required")
    revision = required.revision + 1
    tombstone = TombstoneCreateSpec(
        id=_plan_uuid(context.operation_id, f"tombstone:{required.id}"),
        memory_id=required.id,
        fingerprint=fingerprint,
        memory_type=required.memory_type,
        domain_key=required.domain_key,
        slot_key=required.slot_key,
        created_at=context.now,
        expires_at=tombstone_expiration(context.now),
    )
    scrubbed_fingerprint = f"forgotten:{context.operation_id}"
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.FORGOTTEN,
        operation_status="committed",
        affected_memory_ids=(required.id,),
        current_revision=revision,
        records_to_update=(
            _update(
                required,
                status=MemoryLifecycleState.FORGOTTEN.value,
                canonical_fingerprint=scrubbed_fingerprint,
            ),
        ),
        sources_to_detach=(SourceDetachSpec(required.id, "forgotten"),),
        tombstones_to_create=(tombstone,),
        outbox_events=(
            _outbox(
                context,
                label="forget:remove",
                event_kind="canonical_remove",
                memory_id=required.id,
                revision=revision,
                content_hash=None,
            ),
            _outbox(
                context,
                label="forget:expiry",
                event_kind="tombstone_expiry",
                memory_id=required.id,
                revision=revision,
                content_hash=None,
                payload={
                    "tombstone_id": tombstone.id,
                    "expires_at": tombstone.expires_at.isoformat(),
                },
            ),
        ),
        preconditions=(_precondition(required),),
    )


def _plan_erase(
    command: ErasePermanentlyMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    required = _required_record(
        command,
        context,
        state,
        memory_id=str(command.target.memory_id),
        expected_revision=command.target.expected_revision,
    )
    if isinstance(required, MutationPlan):
        return required
    tombstones = tuple(
        item.id
        for item in state.tombstones
        if item.owner_id == state.owner_id
        and item.memory_type == required.memory_type.value
        and item.domain_key == required.domain_key
        and item.slot_key == required.slot_key
    )
    return MutationPlan(
        operation_id=context.operation_id,
        operation=command.operation,
        outcome=MemoryOutcome.ERASED_PERMANENTLY,
        operation_status="committed",
        affected_memory_ids=(required.id,),
        records_to_delete=(RecordDeleteSpec(required.id, required.revision),),
        tombstones_to_delete=tombstones,
        outbox_events=(
            _outbox(
                context,
                label="erase:reconcile",
                event_kind="reconciliation_request",
                memory_id=None,
                revision=None,
                content_hash=None,
                payload={"action": "canonical_remove", "memory_id": required.id},
            ),
        ),
        preconditions=(_precondition(required),),
    )


def _plan_restore(
    command: RestoreMemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    required = _required_record(
        command,
        context,
        state,
        memory_id=str(command.target.memory_id),
        expected_revision=command.target.expected_revision,
    )
    if isinstance(required, MutationPlan):
        return required
    if command.mode is RestoreMode.ARCHIVED_ONLY:
        if required.status is not MemoryLifecycleState.ARCHIVED:
            return _rejected(
                command,
                context,
                rejection_code=MemoryRejectionCode.INVALID_RESTORE,
                message="only_archived_records_can_be_restored_directly",
            )
        occupied = state.active_slot(
            subject_key=required.subject_key,
            memory_type=required.memory_type,
            domain_key=required.domain_key,
            slot_key=required.slot_key,
        )
        if occupied:
            return _rejected(
                command,
                context,
                rejection_code=MemoryRejectionCode.INVALID_RESTORE,
                message="restore_slot_not_empty",
            )
        revision = required.revision + 1
        return MutationPlan(
            operation_id=context.operation_id,
            operation=command.operation,
            outcome=MemoryOutcome.RESTORED,
            operation_status="committed",
            affected_memory_ids=(required.id,),
            active_memory_ids=(required.id,),
            current_revision=revision,
            records_to_update=(_update(required, status=MemoryLifecycleState.ACTIVE.value),),
            outbox_events=(
                _outbox(
                    context,
                    label="restore",
                    event_kind="canonical_upsert",
                    memory_id=required.id,
                    revision=revision,
                    content_hash=required.canonical_fingerprint,
                ),
            ),
            preconditions=(_precondition(required),),
        )

    active = state.active_slot(
        subject_key=required.subject_key,
        memory_type=required.memory_type,
        domain_key=required.domain_key,
        slot_key=required.slot_key,
    )
    if len(active) != 1:
        return _rejected(
            command,
            context,
            rejection_code=MemoryRejectionCode.INVALID_RESTORE,
            message="restore_as_replacement_requires_one_active_successor",
        )
    return _replacement_plan(command, state, context, active, outcome=MemoryOutcome.RESTORED)


def plan_memory_mutation(
    command: MemoryCommand,
    state: PlannerState,
    context: PlannerContext,
) -> MutationPlan:
    """Return an immutable plan without I/O, persistence, crypto, or hidden decisions."""

    if command.owner_id != state.owner_id:
        return _rejected(
            command,
            context,
            outcome=MemoryOutcome.FAILED,
            error_code=MemoryErrorCode.OWNER_MISMATCH,
            message="owner_mismatch",
        )
    if isinstance(command, CreateMemoryCommand):
        return _plan_create(command, state, context)
    if isinstance(command, UpdateMemoryCommand):
        return _plan_update(command, state, context)
    if isinstance(command, ReplaceMemoryCommand):
        return _plan_replace(command, state, context)
    if isinstance(command, SupersedeMemoryCommand):
        return _plan_supersede(command, state, context)
    if isinstance(command, MergeMemoryCommand):
        return _plan_merge(command, state, context)
    if isinstance(command, ArchiveMemoryCommand):
        return _plan_archive(command, state, context)
    if isinstance(command, ForgetMemoryCommand):
        return _plan_forget(command, state, context)
    if isinstance(command, ErasePermanentlyMemoryCommand):
        return _plan_erase(command, state, context)
    if isinstance(command, RestoreMemoryCommand):
        return _plan_restore(command, state, context)
    raise TypeError(f"unsupported memory command type: {type(command).__name__}")
