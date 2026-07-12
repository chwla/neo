from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.provider_runtime import (
    ProviderRuntimeService,
    RuntimeCompleteRequest,
    RuntimeHealthRequest,
)

router = APIRouter(prefix="/providers/runtime", tags=["provider-runtime"])


def service() -> ProviderRuntimeService:
    return ProviderRuntimeService()


def fail(exc: Exception):
    raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


@router.get("/status")
def status():
    return service().status()


@router.post("/health-check")
def health_check(payload: RuntimeHealthRequest):
    try:
        return service().health_check(**payload.model_dump())
    except (LookupError, ValueError, RuntimeError) as exc:
        fail(exc)


@router.get("/health")
def health():
    return {"checks": service().health()}


@router.get("/requests")
def requests(limit: int = Query(default=100, ge=1, le=500)):
    return {"requests": service().requests(limit)}


@router.get("/requests/{request_id}")
def request(request_id: str):
    value = service().request(request_id)
    if not value:
        raise HTTPException(404, "Provider request not found.")
    return value


@router.post("/complete")
def complete(payload: RuntimeCompleteRequest):
    try:
        return (
            service()
            .complete(
                request_type=payload.request_type,
                route_name=payload.route_name,
                messages=payload.messages,
                max_tokens=payload.max_tokens,
                metadata=payload.metadata,
            )
            .model_dump()
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        fail(exc)


@router.post("/stream/start")
def stream_start(payload: RuntimeCompleteRequest):
    try:
        return service().start_stream(
            request_type=payload.request_type,
            route_name=payload.route_name,
            messages=payload.messages,
            max_tokens=payload.max_tokens,
            metadata=payload.metadata,
        )
    except (LookupError, ValueError, RuntimeError) as exc:
        fail(exc)


@router.get("/stream/{request_id}")
def stream_poll(request_id: str):
    return request(request_id)


@router.post("/stream/{request_id}/cancel")
def stream_cancel(request_id: str):
    value = service().cancel_stream(request_id)
    if not value:
        raise HTTPException(404, "Provider stream not found.")
    return value


@router.get("/rate-limits")
def rate_limits():
    return {"rate_limits": service().rate_limits()}


@router.get("/usage")
def usage():
    return service().usage()
