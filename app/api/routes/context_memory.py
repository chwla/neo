from fastapi import APIRouter, HTTPException

from app.services.context_memory import ContextMemoryService
from app.services.context_memory.types import CompactRequest, ContextEventCreate

router = APIRouter(prefix="/context-memory", tags=["context-memory"])


def service() -> ContextMemoryService:
    return ContextMemoryService()


def fail(exc: Exception):
    raise HTTPException(400 if isinstance(exc, ValueError) else 404, str(exc)) from exc


@router.get("/summaries")
def summaries(scope_type: str | None = None, scope_id: str | None = None):
    try:
        return {"summaries": service().summaries(scope_type, scope_id)}
    except ValueError as exc:
        fail(exc)


@router.get("/summaries/{summary_id}")
def summary(summary_id: str):
    item = service().summary(summary_id)
    if not item:
        raise HTTPException(404, "Context summary not found.")
    return {"summary": item}


@router.post("/preview")
def preview(request: CompactRequest):
    try:
        return service().preview(request)
    except ValueError as exc:
        fail(exc)


@router.post("/compact")
def compact(request: CompactRequest):
    try:
        return service().compact(request)
    except ValueError as exc:
        fail(exc)


@router.get("/scopes/{scope_type}/{scope_id}")
def scope(scope_type: str, scope_id: str):
    try:
        return service().scope(scope_type, scope_id)
    except ValueError as exc:
        fail(exc)


@router.get("/scopes/{scope_type}/{scope_id}/events")
def events(scope_type: str, scope_id: str):
    try:
        return {"events": service().events(scope_type, scope_id)}
    except ValueError as exc:
        fail(exc)


@router.post("/scopes/{scope_type}/{scope_id}/events", status_code=201)
def event(scope_type: str, scope_id: str, request: ContextEventCreate):
    try:
        return service().event(scope_type, scope_id, request)
    except ValueError as exc:
        fail(exc)
