from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.services.llm_registry import store
from app.services.llm_registry.health import check_health
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.types import (
    HealthRequest,
    ModelCreate,
    ModelUpdate,
    ProviderCreate,
    ProviderUpdate,
    RouteUpdate,
)

router = APIRouter(prefix="/llm", tags=["llm-registry"])


def _service() -> LLMRegistryService:
    return LLMRegistryService()


def _raise(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/providers")
def list_providers() -> dict:
    items = _service().list_providers()
    return {"providers": items, "total": len(items)}


@router.post("/providers", status_code=status.HTTP_201_CREATED)
def create_provider(request: ProviderCreate) -> dict:
    try:
        return {"provider": _service().create_provider(request)}
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: str, request: ProviderUpdate) -> dict:
    try:
        return {"provider": _service().update_provider(provider_id, request)}
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: str) -> Response:
    try:
        _service().delete_provider(provider_id)
    except (ValueError, LookupError) as exc:
        _raise(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/models")
def list_models(provider_id: str | None = None) -> dict:
    items = _service().list_models(provider_id)
    return {"models": items, "total": len(items)}


@router.post("/models", status_code=status.HTTP_201_CREATED)
def create_model(request: ModelCreate) -> dict:
    try:
        return {"model": _service().create_model(request)}
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.patch("/models/{model_id}")
def update_model(model_id: str, request: ModelUpdate) -> dict:
    try:
        return {"model": _service().update_model(model_id, request)}
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(model_id: str) -> Response:
    try:
        _service().delete_model(model_id)
    except (ValueError, LookupError) as exc:
        _raise(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/routes")
def list_routes() -> dict:
    items = _service().list_routes()
    return {"routes": items, "total": len(items)}


@router.patch("/routes/{route_name}")
def update_route(route_name: str, request: RouteUpdate) -> dict:
    try:
        return {"route": _service().update_route(route_name, request)}
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.post("/health")
def provider_health(request: HealthRequest) -> dict:
    try:
        return check_health(**request.model_dump())
    except (ValueError, LookupError) as exc:
        _raise(exc)


@router.get("/usage")
def usage(
    route_name: str | None = None,
    provider_id: str | None = None,
    call_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = store.list_calls(
        route_name=route_name,
        provider_id=provider_id,
        status=call_status,
        limit=limit,
        offset=offset,
    )
    return {"calls": items, "total": total}
