from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.services.memory_retrieval import MemoryRetrievalService
from app.services.memory_retrieval.pruning import MemoryPruner
from app.services.memory_retrieval.types import (
    MemoryIndexRequest,
    MemoryItemCreate,
    MemoryItemUpdate,
    MemoryRetrieveRequest,
    PruneRequest,
)

router = APIRouter(prefix="/memory", tags=["memory-retrieval"])


def service() -> MemoryRetrievalService:
    return MemoryRetrievalService()


@router.post("/index")
def index(request: MemoryIndexRequest):
    return service().index(request)


@router.post("/retrieve")
def retrieve(request: MemoryRetrieveRequest):
    return service().retrieve(request)


@router.get("/items")
def items(
    scope_type: str | None = None,
    scope_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=300),
):
    values = service().list_items(scope_type=scope_type, scope_id=scope_id, limit=limit)
    return {"items": values, "total": len(values)}


@router.post("/items", status_code=201)
def create_item(request: MemoryItemCreate):
    return service().create(request)


@router.get("/items/{item_id}")
def item(item_id: str):
    value = service().item(item_id)
    if not value:
        raise HTTPException(404, "Memory item not found.")
    return value


@router.patch("/items/{item_id}")
def update_item(item_id: str, request: MemoryItemUpdate):
    try:
        return service().update(item_id, request)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: str):
    if not service().delete(item_id):
        raise HTTPException(404, "Memory item not found.")
    return Response(status_code=204)


@router.get("/scopes/{scope_type}/{scope_id}")
def scope_items(scope_type: str, scope_id: str):
    values = service().list_items(scope_type=scope_type, scope_id=scope_id)
    return {"items": values, "total": len(values)}


@router.get("/retrievals")
def retrievals(limit: int = Query(default=100, ge=1, le=300)):
    from app.services.memory_retrieval.audit import list_retrievals

    values = list_retrievals(limit)
    return {"retrievals": values, "total": len(values)}


@router.get("/retrievals/{retrieval_id}")
def retrieval(retrieval_id: str):
    from app.services.memory_retrieval.audit import get_retrieval

    value = get_retrieval(retrieval_id)
    if not value:
        raise HTTPException(404, "Memory retrieval not found.")
    return value


@router.post("/prune/preview")
def prune_preview(request: PruneRequest):
    return MemoryPruner().preview(request.stale_days)


@router.post("/prune/apply")
def prune_apply(request: PruneRequest):
    try:
        return MemoryPruner().apply(request.stale_days, confirm=request.confirm)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
