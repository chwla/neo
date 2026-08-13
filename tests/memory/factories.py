"""Builders for memory contracts and for raw rows.

Two kinds of helper live here.  The ``*_proposal`` / ``*_command`` builders make
valid contract objects with sane defaults, so a test only states the field it
cares about.  The ``insert_*`` helpers write rows directly, for tests that need
a starting state without paying for a full mutation — and for the constraint
tests, which need to attempt writes the mutation layer would never make.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert
from sqlalchemy.engine import Engine

from app.models.memory import (
    MemoryCandidate as MemoryCandidateRow,
)
from app.models.memory import (
    MemoryOperation as MemoryOperationRow,
)
from app.models.memory import (
    MemoryRecord as MemoryRecordRow,
)
from app.services.memory.contracts import (
    ActorKind,
    ArchiveMemoryCommand,
    CandidateIntent,
    CandidateTargetHints,
    CreateMemoryCommand,
    ErasePermanentlyMemoryCommand,
    EvidenceRole,
    EvidenceSpan,
    ForgetMemoryCommand,
    MemoryActor,
    MemoryLifecycleState,
    MemoryOperationKind,
    MemorySource,
    MemoryUpdatePatch,
    MergeMemoryCommand,
    ReplaceMemoryCommand,
    ReplacementAuthority,
    RestoreMemoryCommand,
    Sensitivity,
    SourceKind,
    SupersedeMemoryCommand,
    TargetRevision,
    UpdateMemoryCommand,
    ValidatedCandidateProposal,
)
from app.services.memory.taxonomy import Cardinality, MemoryType
from app.services.memory.versions import CONTRACT_VERSION, POLICY_VERSION, TAXONOMY_VERSION
from tests.memory.conftest import FROZEN_NOW, OWNER_ID

DEFAULT_DOMAIN = "global"
DEFAULT_GOAL_ENTITY = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEFAULT_GOAL_SLOT = f"goal:{DEFAULT_DOMAIN}:independent:{DEFAULT_GOAL_ENTITY}"
DEFAULT_PREFERENCE_SLOT = f"preference:{DEFAULT_DOMAIN}:verbosity"
DEFAULT_IDENTITY_SLOT = "identity:global:name"


# --------------------------------------------------------------------------
# Contract builders
# --------------------------------------------------------------------------


def actor(kind: ActorKind = ActorKind.USER, actor_id: str = "test-user") -> MemoryActor:
    return MemoryActor(kind=kind, actor_id=actor_id)


def source(
    kind: SourceKind = SourceKind.DIRECT_COMMAND,
    *,
    message_id: str = "message-1",
    evidence: tuple[EvidenceSpan, ...] | None = None,
    **overrides: Any,
) -> MemorySource:
    return MemorySource(
        kind=kind,
        message_id=message_id,
        observed_at=FROZEN_NOW,
        evidence=evidence if evidence is not None else (),
        **overrides,
    )


def evidence_span(
    text: str = "I want to improve at urban sketching",
    *,
    role: EvidenceRole = EvidenceRole.ASSERTION,
    start: int | None = None,
    end: int | None = None,
) -> EvidenceSpan:
    return EvidenceSpan(role=role, text=text, start=start, end=end)


def proposal(
    *,
    memory_type: MemoryType = MemoryType.GOAL,
    domain_key: str = DEFAULT_DOMAIN,
    slot_key: str | None = None,
    cardinality: Cardinality = Cardinality.ADDITIVE,
    canonical_value: Any = "improve at urban sketching",
    display_text: str = "improve at urban sketching",
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    confidence: float = 0.9,
    importance: int = 5,
    intent: CandidateIntent = CandidateIntent.ASSERT,
    explicit_user_request: bool = False,
    target_hints: CandidateTargetHints | None = None,
    **overrides: Any,
) -> ValidatedCandidateProposal:
    """A validated proposal with defaults that pass normalization."""

    return ValidatedCandidateProposal(
        memory_type=memory_type,
        domain_key=domain_key,
        slot_key=slot_key if slot_key is not None else DEFAULT_GOAL_SLOT,
        cardinality=cardinality,
        canonical_value=canonical_value,
        display_text=display_text,
        sensitivity=sensitivity,
        confidence=confidence,
        importance=importance,
        intent=intent,
        explicit_user_request=explicit_user_request,
        target_hints=target_hints if target_hints is not None else CandidateTargetHints(),
        **overrides,
    )


def preference_proposal(
    *,
    dimension: str = "verbosity",
    domain_key: str = DEFAULT_DOMAIN,
    canonical_value: Any = "concise answers",
    display_text: str = "concise answers",
    **overrides: Any,
) -> ValidatedCandidateProposal:
    return proposal(
        memory_type=MemoryType.PREFERENCE,
        domain_key=domain_key,
        slot_key=f"preference:{domain_key}:{dimension}",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=canonical_value,
        display_text=display_text,
        **overrides,
    )


def identity_proposal(
    *,
    identity_key: str = "name",
    canonical_value: Any = "Soham",
    display_text: str = "Soham",
    **overrides: Any,
) -> ValidatedCandidateProposal:
    return proposal(
        memory_type=MemoryType.IDENTITY,
        domain_key="global",
        slot_key=f"identity:global:{identity_key}",
        cardinality=Cardinality.EXCLUSIVE,
        canonical_value=canonical_value,
        display_text=display_text,
        **overrides,
    )


def _command_defaults(owner: str, idempotency_key: str) -> dict[str, Any]:
    return {
        "owner_id": owner,
        "idempotency_key": idempotency_key,
        "actor": actor(),
        "source": source(),
    }


def create_command(
    *,
    owner: str = OWNER_ID,
    idempotency_key: str = "create-1",
    candidate: ValidatedCandidateProposal | None = None,
    **overrides: Any,
) -> CreateMemoryCommand:
    return CreateMemoryCommand(
        **_command_defaults(owner, idempotency_key),
        candidate=candidate if candidate is not None else proposal(),
        **overrides,
    )


def update_command(
    *,
    memory_id: UUID,
    expected_revision: int = 1,
    owner: str = OWNER_ID,
    idempotency_key: str = "update-1",
    patch: MemoryUpdatePatch | None = None,
    **overrides: Any,
) -> UpdateMemoryCommand:
    return UpdateMemoryCommand(
        **_command_defaults(owner, idempotency_key),
        target=TargetRevision(memory_id=memory_id, expected_revision=expected_revision),
        patch=patch if patch is not None else MemoryUpdatePatch(importance=7),
        **overrides,
    )


def replace_command(
    *,
    owner: str = OWNER_ID,
    idempotency_key: str = "replace-1",
    candidate: ValidatedCandidateProposal | None = None,
    authority: ReplacementAuthority = ReplacementAuthority.EXPLICIT_CORRECTION,
    targets: tuple[TargetRevision, ...] = (),
    **overrides: Any,
) -> ReplaceMemoryCommand:
    return ReplaceMemoryCommand(
        **_command_defaults(owner, idempotency_key),
        candidate=(
            candidate if candidate is not None else proposal(intent=CandidateIntent.REPLACE)
        ),
        authority=authority,
        targets=targets,
        **overrides,
    )


def supersede_command(
    *,
    predecessors: tuple[TargetRevision, ...],
    successor_memory_id: UUID,
    owner: str = OWNER_ID,
    idempotency_key: str = "supersede-1",
    **overrides: Any,
) -> SupersedeMemoryCommand:
    return SupersedeMemoryCommand(
        **_command_defaults(owner, idempotency_key),
        predecessors=predecessors,
        successor_memory_id=successor_memory_id,
        **overrides,
    )


def merge_command(
    *,
    sources: tuple[TargetRevision, ...],
    owner: str = OWNER_ID,
    idempotency_key: str = "merge-1",
    candidate: ValidatedCandidateProposal | None = None,
    **overrides: Any,
) -> MergeMemoryCommand:
    return MergeMemoryCommand(
        **_command_defaults(owner, idempotency_key),
        sources=sources,
        candidate=candidate if candidate is not None else proposal(),
        **overrides,
    )


def _target_command(
    factory: type,
    *,
    memory_id: UUID,
    expected_revision: int,
    owner: str,
    idempotency_key: str,
    **overrides: Any,
):
    return factory(
        **_command_defaults(owner, idempotency_key),
        target=TargetRevision(memory_id=memory_id, expected_revision=expected_revision),
        **overrides,
    )


def archive_command(
    *,
    memory_id: UUID,
    expected_revision: int = 1,
    owner: str = OWNER_ID,
    idempotency_key: str = "archive-1",
    **overrides: Any,
) -> ArchiveMemoryCommand:
    return _target_command(
        ArchiveMemoryCommand,
        memory_id=memory_id,
        expected_revision=expected_revision,
        owner=owner,
        idempotency_key=idempotency_key,
        **overrides,
    )


def forget_command(
    *,
    memory_id: UUID,
    expected_revision: int = 1,
    owner: str = OWNER_ID,
    idempotency_key: str = "forget-1",
    **overrides: Any,
) -> ForgetMemoryCommand:
    return _target_command(
        ForgetMemoryCommand,
        memory_id=memory_id,
        expected_revision=expected_revision,
        owner=owner,
        idempotency_key=idempotency_key,
        **overrides,
    )


def erase_command(
    *,
    memory_id: UUID,
    expected_revision: int = 1,
    owner: str = OWNER_ID,
    idempotency_key: str = "erase-1",
    **overrides: Any,
) -> ErasePermanentlyMemoryCommand:
    return _target_command(
        ErasePermanentlyMemoryCommand,
        memory_id=memory_id,
        expected_revision=expected_revision,
        owner=owner,
        idempotency_key=idempotency_key,
        **overrides,
    )


def restore_command(
    *,
    memory_id: UUID,
    expected_revision: int = 1,
    owner: str = OWNER_ID,
    idempotency_key: str = "restore-1",
    **overrides: Any,
) -> RestoreMemoryCommand:
    return _target_command(
        RestoreMemoryCommand,
        memory_id=memory_id,
        expected_revision=expected_revision,
        owner=owner,
        idempotency_key=idempotency_key,
        **overrides,
    )


# --------------------------------------------------------------------------
# Raw row builders
# --------------------------------------------------------------------------


def operation_values(
    *,
    operation_id: str | None = None,
    owner: str = OWNER_ID,
    idempotency_key: str = "seed-operation",
    operation_kind: MemoryOperationKind | str = MemoryOperationKind.CREATE,
    **overrides: Any,
) -> dict[str, Any]:
    # Constraint tests deliberately pass raw strings that are not valid enum
    # members, so this must not assume ``.value`` exists.
    kind = getattr(operation_kind, "value", operation_kind)
    values: dict[str, Any] = {
        # ``is not None`` rather than truthiness: a test passing "" wants the
        # empty string written, not a generated UUID substituted for it.
        "id": operation_id if operation_id is not None else str(uuid4()),
        "owner_id": owner,
        "idempotency_key": idempotency_key,
        "operation_kind": kind,
        "actor_kind": ActorKind.USER.value,
        "actor_id": "test-user",
        "source_kind": SourceKind.DIRECT_COMMAND.value,
        "sensitivity": Sensitivity.NORMAL.value,
        "normalized_command_json": {"operation": kind},
        "request_hash": "sha256:" + "0" * 64,
        "status": "committed",
        "result_record_ids": [],
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "created_at": FROZEN_NOW,
        "committed_at": FROZEN_NOW,
    }
    values.update(overrides)
    return values


def record_values(
    *,
    record_id: str | None = None,
    owner: str = OWNER_ID,
    operation_id: str,
    subject_key: str = "user",
    memory_type: MemoryType = MemoryType.GOAL,
    scope_type: str = "global",
    scope_project_id: str | None = None,
    domain_key: str = DEFAULT_DOMAIN,
    slot_key: str = DEFAULT_GOAL_SLOT,
    cardinality: Cardinality = Cardinality.ADDITIVE,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    canonical_payload: Any = "improve at urban sketching",
    display_text: str = "improve at urban sketching",
    canonical_fingerprint: str | None = None,
    confidence: float = 0.9,
    importance: int = 5,
    status: MemoryLifecycleState = MemoryLifecycleState.ACTIVE,
    now: datetime | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    moment = now or FROZEN_NOW
    values: dict[str, Any] = {
        "id": record_id if record_id is not None else str(uuid4()),
        "owner_id": owner,
        "subject_key": subject_key,
        "memory_type": memory_type.value,
        "scope_type": scope_type,
        "scope_project_id": scope_project_id,
        "domain_key": domain_key,
        "slot_key": slot_key,
        "cardinality": cardinality.value,
        "sensitivity": sensitivity.value,
        "canonical_payload": canonical_payload,
        "display_text": display_text,
        "canonical_fingerprint": (
            canonical_fingerprint
            if canonical_fingerprint is not None
            else f"sha256:{uuid4().hex * 2}"
        ),
        "confidence": confidence,
        "importance": importance,
        "status": status.value,
        "created_at": moment,
        "updated_at": moment,
        "last_confirmed_at": moment,
        "usage_count": 0,
        "pinned": False,
        "created_by_operation_id": operation_id,
        "revision": 1,
        "metadata_json": {},
        "contract_version": CONTRACT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "policy_version": POLICY_VERSION,
        "value_schema_version": 1,
    }
    values.update(overrides)
    return values


def insert_operation(engine: Engine, **overrides: Any) -> str:
    """Write one committed operation row and return its id."""

    values = operation_values(**overrides)
    with engine.begin() as connection:
        connection.execute(insert(MemoryOperationRow).values(**values))
    return values["id"]


def insert_record(engine: Engine, *, operation_id: str | None = None, **overrides: Any) -> str:
    """Write one record row (creating its operation if needed) and return its id."""

    resolved_operation = operation_id
    if resolved_operation is None:
        resolved_operation = insert_operation(
            engine,
            owner=overrides.get("owner", OWNER_ID),
            idempotency_key=f"seed-{uuid4()}",
        )
    values = record_values(operation_id=resolved_operation, **overrides)
    with engine.begin() as connection:
        connection.execute(insert(MemoryRecordRow).values(**values))
    return values["id"]


def candidate_values(
    *,
    candidate_id: str | None = None,
    owner: str = OWNER_ID,
    subject_key: str = "user",
    memory_type: MemoryType = MemoryType.GOAL,
    domain_key: str = DEFAULT_DOMAIN,
    slot_key: str = DEFAULT_GOAL_SLOT,
    cardinality: Cardinality = Cardinality.ADDITIVE,
    sensitivity: Sensitivity = Sensitivity.NORMAL,
    canonical_payload: Any = "improve at urban sketching",
    display_text: str = "improve at urban sketching",
    intent: CandidateIntent = CandidateIntent.ASSERT,
    state: str = "validated",
    explicit_user_request: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": candidate_id if candidate_id is not None else str(uuid4()),
        "owner_id": owner,
        "subject_key": subject_key,
        "memory_type": memory_type.value,
        "scope_type": "global",
        "scope_project_id": None,
        "domain_key": domain_key,
        "slot_key": slot_key,
        "cardinality": cardinality.value,
        "sensitivity": sensitivity.value,
        "canonical_payload": canonical_payload,
        "display_text": display_text,
        "intent": intent.value,
        "target_hints_json": {},
        "trusted_target_ids": [],
        "predecessor_evidence_json": {},
        "source_spans_json": [],
        "grounding_evidence_json": {},
        "confidence": 0.9,
        "importance": 5,
        "explicit_user_request": explicit_user_request,
        "extractor_name": "test-extractor",
        "extractor_version": "v1",
        "state": state,
        "created_at": FROZEN_NOW,
        "updated_at": FROZEN_NOW,
        "revision": 1,
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "value_schema_version": 1,
    }
    values.update(overrides)
    return values


def insert_candidate(engine: Engine, **overrides: Any) -> str:
    values = candidate_values(**overrides)
    with engine.begin() as connection:
        connection.execute(insert(MemoryCandidateRow).values(**values))
    return values["id"]


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)
