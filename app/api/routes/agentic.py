from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.agentic_core import (
    AgenticContinueRequest,
    AgenticCoreError,
    AgenticCoreService,
    AgenticPlanUpdate,
    AgenticRunCreate,
    AgenticStepRequest,
)

router = APIRouter(prefix="/agentic", tags=["agentic-core"])


def _service() -> AgenticCoreService:
    return AgenticCoreService()


def _raise(exc: Exception) -> None:
    raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


@router.post("/runs", status_code=201)
def start_run(payload: AgenticRunCreate):
    try:
        return _service().start(payload)
    except (AgenticCoreError, LookupError, RuntimeError, ValueError) as exc:
        _raise(exc)


@router.get("/runs")
def list_runs(
    run_type: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    return _service().list(run_type=run_type, status=status, limit=limit, offset=offset)


@router.get("/runs/{run_id}")
def read_run(run_id: str):
    try:
        return _service().detail(run_id)
    except LookupError as exc:
        _raise(exc)


@router.post("/runs/{run_id}/plan")
def plan_run(run_id: str, payload: AgenticPlanUpdate | None = None):
    try:
        return _service().plan(run_id, payload)
    except (AgenticCoreError, LookupError, ValueError) as exc:
        _raise(exc)


@router.post("/runs/{run_id}/step")
def run_step(run_id: str, payload: AgenticStepRequest | None = None):
    try:
        return _service().execute_step(run_id, payload)
    except (AgenticCoreError, LookupError, RuntimeError, ValueError) as exc:
        _raise(exc)


@router.post("/runs/{run_id}/continue")
def continue_run(run_id: str, payload: AgenticContinueRequest | None = None):
    try:
        return _service().continue_run(run_id, payload.note if payload else None)
    except (AgenticCoreError, LookupError, RuntimeError, ValueError) as exc:
        _raise(exc)


@router.post("/runs/{run_id}/reflect")
def reflect_run(run_id: str):
    try:
        return _service().reflect(run_id)
    except (AgenticCoreError, LookupError, RuntimeError, ValueError) as exc:
        _raise(exc)


@router.post("/runs/{run_id}/stop")
def stop_run(run_id: str):
    try:
        return _service().stop(run_id)
    except (AgenticCoreError, LookupError, ValueError) as exc:
        _raise(exc)


@router.get("/runs/{run_id}/steps")
def run_steps(run_id: str):
    try:
        detail = _service().detail(run_id)
        return {"steps": detail["steps"], "total": len(detail["steps"])}
    except LookupError as exc:
        _raise(exc)


@router.get("/runs/{run_id}/context")
def run_context(run_id: str):
    try:
        return _service().context(run_id)
    except (LookupError, RuntimeError, ValueError) as exc:
        _raise(exc)
