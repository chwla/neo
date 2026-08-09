from __future__ import annotations

from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes.accounts import session_for
from app.db.session import get_db
from app.models.memory import MemoryCandidate, MemoryRecord
from app.models.project import Project
from app.services.memory.adapters import StructuredMemoryInput
from app.services.memory.contracts import (
    CandidateIntent,
    MemoryOutcome,
    MemoryRejectionCode,
    MemoryUpdatePatch,
    ReplacementAuthority,
    Sensitivity,
    SourceKind,
    TargetRevision,
)
from app.services.memory.factory import MemoryRuntime, build_memory_runtime
from app.services.memory.taxonomy import Cardinality, MemoryType

router = APIRouter(prefix="/memory", tags=["memory"])
Database = Annotated[Session, Depends(get_db)]


class MemoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    display_text: str = Field(min_length=1, max_length=12_000)
    memory_type: MemoryType = MemoryType.KNOWLEDGE
    scope_type: str = Field(default="global", pattern="^(global|project)$")
    project_id: str | None = Field(default=None, max_length=80)
    # Kept as a compatibility-only taxonomy hint for existing API callers.  Scope
    # is authoritative; the redesigned UI never exposes this free-text field.
    domain: str = Field(default="global", min_length=1, max_length=200)
    slot: str | None = Field(default=None, max_length=400)
    cardinality: Cardinality = Cardinality.ADDITIVE
    importance: int = Field(default=7, ge=1, le=10)
    confidence: float = Field(default=1.0, ge=0, le=1)
    client_mutation_id: str | None = Field(default=None, min_length=1, max_length=200)


class MemoryPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any | None = None
    display_text: str | None = Field(default=None, min_length=1, max_length=12_000)
    importance: int | None = Field(default=None, ge=1, le=10)
    confidence: float | None = Field(default=None, ge=0, le=1)
    pinned: bool | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class CandidateDecisionRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=1)


def _profile(request: Request) -> dict:
    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="authenticated_profile_required")
    return profile


def _runtime(request: Request) -> MemoryRuntime:
    try:
        return build_memory_runtime(_profile(request))
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="memory_runtime_unavailable") from exc


def _context(runtime: MemoryRuntime, request_id: str):
    return runtime.context(
        source_kind=SourceKind.MANUAL_UI,
        source_id="memories-ui",
        request_id=request_id,
    )


def _scope(payload: MemoryCreateRequest, db: Session) -> tuple[str, str | None, str | None]:
    if payload.scope_type == "global":
        if payload.project_id is not None:
            raise HTTPException(status_code=422, detail="global_scope_cannot_have_project")
        return "global", None, None
    if not payload.project_id:
        raise HTTPException(status_code=422, detail="project_scope_requires_project")
    project = db.scalar(select(Project).where(Project.id == int(payload.project_id))) if payload.project_id.isdigit() else None
    if project is None:
        raise HTTPException(status_code=422, detail="project_scope_project_not_found")
    return "project", str(project.id), project.name


def _scope_response(row: MemoryRecord | MemoryCandidate | None, db: Session) -> dict[str, Any]:
    if row is None or row.scope_type != "project" or not row.scope_project_id:
        return {"type": "global"}
    project = db.scalar(select(Project).where(Project.id == int(row.scope_project_id))) if row.scope_project_id.isdigit() else None
    return {"type": "project", "project_id": row.scope_project_id, "project_name": project.name if project else "Deleted project"}


def _default_slot(payload: MemoryCreateRequest, mutation_id: str) -> str:
    entity_id = uuid5(NAMESPACE_URL, f"neo.memory.manual:{mutation_id}")
    memory_type = payload.memory_type.value
    domain = payload.domain
    if payload.memory_type is MemoryType.PREFERENCE:
        return f"preference:{domain}:manual"
    if payload.memory_type is MemoryType.IDENTITY:
        return "identity:global:manual_fact"
    if payload.memory_type is MemoryType.GOAL:
        if payload.cardinality is Cardinality.EXCLUSIVE:
            return f"goal:{domain}:current_primary_goal"
        return f"goal:{domain}:independent:{entity_id}"
    if payload.cardinality is Cardinality.EXCLUSIVE and payload.memory_type in {
        MemoryType.EDUCATION,
        MemoryType.EMPLOYMENT,
    }:
        return f"{memory_type}:{domain}:current_status"
    return f"{memory_type}:{domain}:item:{entity_id}"


def _records(runtime: MemoryRuntime, db: Session) -> list[dict[str, Any]]:
    snapshots = runtime.adapter.list_active_memories(_context(runtime, f"list:{uuid4()}"))
    if not snapshots:
        return []
    rows = {
        row.id: row
        for row in db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == runtime.execution.owner_id,
                MemoryRecord.id.in_([str(item.memory_id) for item in snapshots]),
            )
        )
    }
    result = []
    for item in snapshots:
        row = rows.get(str(item.memory_id))
        result.append(
            {
                "id": str(item.memory_id),
                "memory_type": item.memory_type.value,
                "domain": item.domain_key,
                "scope": _scope_response(row, db),
                "slot": item.slot_key,
                "cardinality": item.cardinality.value,
                "canonical_value": item.canonical_value,
                "display_text": item.display_text,
                "status": item.status.value,
                "revision": item.revision,
                "sensitivity": item.sensitivity.value,
                "created_at": row.created_at if row else None,
                "updated_at": row.updated_at if row else None,
            }
        )
    return result


def _record_or_404(runtime: MemoryRuntime, db: Session, memory_id: UUID) -> dict[str, Any]:
    record = next((item for item in _records(runtime, db) if item["id"] == str(memory_id)), None)
    if record is None:
        raise HTTPException(status_code=404, detail="memory_not_found")
    return record


def _ensure_applied(result) -> None:
    mutation = result.mutation
    if mutation is None:
        raise HTTPException(status_code=409, detail=result.reason or "memory_not_applied")
    if mutation.outcome in {
        MemoryOutcome.REJECTED,
        MemoryOutcome.NEEDS_REVIEW,
        MemoryOutcome.FAILED,
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "outcome": mutation.outcome.value,
                "rejection_code": mutation.rejection_code.value
                if mutation.rejection_code
                else None,
                "error_code": mutation.error_code.value if mutation.error_code else None,
            },
        )


@router.get("")
def list_memories(request: Request, db: Database) -> list[dict[str, Any]]:
    return _records(_runtime(request), db)


@router.post("", status_code=status.HTTP_201_CREATED)
def create_memory(payload: MemoryCreateRequest, request: Request, db: Database) -> dict[str, Any]:
    runtime = _runtime(request)
    mutation_id = payload.client_mutation_id or str(uuid4())
    scope_type, project_id, _project_name = _scope(payload, db)
    slot = payload.slot or _default_slot(payload, mutation_id)
    result = runtime.adapter.create(
        _context(runtime, f"create:{mutation_id}"),
        StructuredMemoryInput(
            memory_type=payload.memory_type,
            domain_key=payload.domain,
            slot_key=slot,
            cardinality=payload.cardinality,
            canonical_value=payload.value,
            display_text=payload.display_text,
            scope_type=scope_type,
            scope_project_id=project_id,
            confidence=payload.confidence,
            importance=payload.importance,
            explicit_user_request=True,
        ),
        idempotency_key=f"manual:{runtime.execution.owner_id}:{mutation_id}",
    )
    _ensure_applied(result)
    db.expire_all()
    memory_id = result.mutation.active_memory_ids[-1]
    return _record_or_404(runtime, db, memory_id)


@router.get("/candidates")
def list_candidates(request: Request, db: Database) -> list[dict[str, Any]]:
    runtime = _runtime(request)
    runtime.adapter.list_active_memories(_context(runtime, f"candidates:{uuid4()}"), limit=1)
    rows = db.scalars(
        select(MemoryCandidate)
        .where(
            MemoryCandidate.owner_id == runtime.execution.owner_id,
            MemoryCandidate.state.in_(("validated", "needs_review")),
        )
        .order_by(MemoryCandidate.created_at.desc())
    )
    return [
        {
            "id": row.id,
            "memory_type": row.memory_type,
            "domain": row.domain_key,
            "scope": _scope_response(row, db),
            "slot": row.slot_key,
            "display_text": row.display_text
            if row.sensitivity == "normal"
            else "[sensitive memory]",
            "state": row.state,
            "intent": row.intent,
            "revision": row.revision,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post("/candidates/{candidate_id}/accept")
def accept_candidate(
    candidate_id: UUID,
    payload: CandidateDecisionRequest,
    request: Request,
    db: Database,
) -> dict[str, Any]:
    runtime = _runtime(request)
    runtime.adapter.list_active_memories(_context(runtime, f"candidate:{candidate_id}"), limit=1)
    candidate = db.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.owner_id == runtime.execution.owner_id,
            MemoryCandidate.id == str(candidate_id),
        )
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate_not_found")
    if payload.expected_revision is not None and candidate.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="candidate_revision_conflict")
    if candidate.sensitivity != Sensitivity.NORMAL.value:
        raise HTTPException(status_code=409, detail="sensitive_candidate_requires_explicit_reentry")
    item = StructuredMemoryInput(
        memory_type=MemoryType(candidate.memory_type),
        domain_key=candidate.domain_key,
        slot_key=candidate.slot_key,
        cardinality=Cardinality(candidate.cardinality),
        canonical_value=candidate.canonical_payload,
        display_text=candidate.display_text,
        scope_type=candidate.scope_type,
        scope_project_id=candidate.scope_project_id,
        sensitivity=Sensitivity(candidate.sensitivity),
        confidence=candidate.confidence,
        importance=candidate.importance,
        explicit_user_request=True,
        proposal_id=candidate_id,
    )
    context = _context(runtime, f"candidate-accept:{candidate_id}:{candidate.revision}")
    targets = tuple(
        TargetRevision(memory_id=UUID(row.id), expected_revision=row.revision)
        for row in db.scalars(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == runtime.execution.owner_id,
                MemoryRecord.id.in_(candidate.trusted_target_ids or []),
            )
        )
    )
    if CandidateIntent(candidate.intent) is CandidateIntent.REPLACE and targets:
        result = runtime.adapter.replace(
            context,
            item,
            targets,
            authority=ReplacementAuthority.REVIEWED,
            idempotency_key=f"candidate-accept:{candidate_id}:{candidate.revision}",
        )
    else:
        result = runtime.adapter.create(
            context,
            item,
            idempotency_key=f"candidate-accept:{candidate_id}:{candidate.revision}",
        )
    _ensure_applied(result)
    return result.mutation.model_dump(mode="json")


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate(
    candidate_id: UUID,
    payload: CandidateDecisionRequest,
    request: Request,
    db: Database,
) -> dict[str, Any]:
    runtime = _runtime(request)
    candidate = db.scalar(
        select(MemoryCandidate).where(
            MemoryCandidate.owner_id == runtime.execution.owner_id,
            MemoryCandidate.id == str(candidate_id),
        )
    )
    if candidate is None:
        raise HTTPException(status_code=404, detail="candidate_not_found")
    result = runtime.coordinator.reject_candidate(
        runtime.execution,
        candidate_id,
        expected_revision=payload.expected_revision or candidate.revision,
    )
    return result.model_dump(mode="json")


@router.get("/{memory_id}")
def get_memory(memory_id: UUID, request: Request, db: Database) -> dict[str, Any]:
    return _record_or_404(_runtime(request), db, memory_id)


@router.patch("/{memory_id}")
def update_memory(
    memory_id: UUID,
    payload: MemoryPatchRequest,
    request: Request,
    db: Database,
) -> dict[str, Any]:
    runtime = _runtime(request)
    current = _record_or_404(runtime, db, memory_id)
    changes = payload.model_dump(exclude={"expected_revision"}, exclude_unset=True)
    if "value" in changes:
        changes["canonical_value"] = changes.pop("value")
    if not changes:
        raise HTTPException(status_code=422, detail="memory_update_requires_a_change")
    expected_revision = payload.expected_revision or current["revision"]
    context = _context(runtime, f"update:{memory_id}:{current['revision']}")
    target = TargetRevision(memory_id=memory_id, expected_revision=expected_revision)
    result = runtime.adapter.update(
        context,
        target,
        MemoryUpdatePatch(**changes),
        idempotency_key=f"manual-update:{memory_id}:{expected_revision}",
    )
    requires_replacement = (
        "canonical_value" in changes
        and current["cardinality"] == Cardinality.EXCLUSIVE.value
        and result.mutation is not None
        and result.mutation.rejection_code is MemoryRejectionCode.CONFLICT_REQUIRES_REPLACE
    )
    if requires_replacement:
        # A manual value edit is an explicit user correction.  Exclusive slots
        # cannot safely overwrite an incompatible assertion in place: use the
        # same replacement lifecycle as chat corrections so the predecessor is
        # superseded, indexed for removal, and never becomes recall-eligible.
        record = db.scalar(
            select(MemoryRecord).where(
                MemoryRecord.owner_id == runtime.execution.owner_id,
                MemoryRecord.id == str(memory_id),
            )
        )
        if record is None:
            raise HTTPException(status_code=404, detail="memory_not_found")
        result = runtime.adapter.replace(
            context,
            StructuredMemoryInput(
                memory_type=MemoryType(current["memory_type"]),
                domain_key=current["domain"],
                slot_key=current["slot"],
                cardinality=Cardinality(current["cardinality"]),
                canonical_value=changes["canonical_value"],
                display_text=changes.get("display_text", current["display_text"]),
                scope_type=current["scope"]["type"],
                scope_project_id=current["scope"].get("project_id"),
                sensitivity=Sensitivity(current["sensitivity"]),
                confidence=record.confidence,
                importance=record.importance,
                explicit_user_request=True,
            ),
            (target,),
            authority=ReplacementAuthority.EXPLICIT_CORRECTION,
            idempotency_key=f"manual-replace:{memory_id}:{expected_revision}",
        )
    _ensure_applied(result)
    db.expire_all()
    active_memory_id = result.mutation.active_memory_ids[-1]
    return _record_or_404(runtime, db, active_memory_id)


@router.delete("/{memory_id}")
def forget_memory(memory_id: UUID, request: Request, db: Database) -> dict[str, Any]:
    runtime = _runtime(request)
    current = _record_or_404(runtime, db, memory_id)
    result = runtime.adapter.forget(
        _context(runtime, f"forget:{memory_id}:{current['revision']}"),
        TargetRevision(memory_id=memory_id, expected_revision=current["revision"]),
        idempotency_key=f"manual-forget:{memory_id}:{current['revision']}",
    )
    _ensure_applied(result)
    return result.mutation.model_dump(mode="json")
