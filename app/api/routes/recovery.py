from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.recovery.service import RecoveryService, RecoveryValidationError
from app.services.recovery.types import (
    ConfirmRequest,
    ForkRunRequest,
    RepairStateRequest,
    RetryRunRequest,
)

router = APIRouter(prefix="/recovery", tags=["recovery"])


def _service() -> RecoveryService:
    return RecoveryService()


def _raise(exc: Exception) -> None:
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/runs")
def runs(
    run_type: str | None = None,
    scan: bool = False,
    limit: int = Query(100, ge=1, le=500),
):
    try:
        service = _service()
        scan_result = service.scan() if scan else None
        result = service.list_runs(run_type=run_type, limit=limit)
        if scan_result is not None:
            result["scan"] = scan_result
        return result
    except (ValueError, RecoveryValidationError) as exc:
        _raise(exc)


@router.get("/runs/{run_type}/{run_id}")
def run_detail(run_type: str, run_id: str):
    try:
        return _service().detail(run_type, run_id)
    except (ValueError, LookupError, RecoveryValidationError) as exc:
        _raise(exc)


@router.post("/runs/{run_type}/{run_id}/resume")
def resume(run_type: str, run_id: str, request: ConfirmRequest):
    try:
        return _service().resume(run_type, run_id, confirm=request.confirm)
    except (ValueError, LookupError, RecoveryValidationError) as exc:
        _raise(exc)


@router.post("/runs/{run_type}/{run_id}/retry")
def retry(run_type: str, run_id: str, request: RetryRunRequest):
    try:
        return _service().retry(
            run_type,
            run_id,
            confirm=request.confirm,
            instructions=request.instructions,
            test_command_id=request.test_command_id,
        )
    except (ValueError, LookupError, RecoveryValidationError) as exc:
        _raise(exc)


@router.post("/runs/{run_type}/{run_id}/fork")
def fork(run_type: str, run_id: str, request: ForkRunRequest):
    try:
        return _service().fork(
            run_type,
            run_id,
            confirm=request.confirm,
            from_step_id=request.from_step_id,
            from_action_request_id=request.from_action_request_id,
            objective_override=request.objective_override,
        )
    except (ValueError, LookupError, RecoveryValidationError) as exc:
        _raise(exc)


@router.post("/runs/{run_type}/{run_id}/repair-state")
def repair_state(run_type: str, run_id: str, request: RepairStateRequest):
    try:
        return _service().repair_state(
            run_type, run_id, confirm=request.confirm, target_status=request.target_status
        )
    except (ValueError, LookupError, RecoveryValidationError) as exc:
        _raise(exc)


@router.get("/events")
def events(
    run_type: str | None = None,
    run_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return _service().list_events(run_type=run_type, run_id=run_id, limit=limit, offset=offset)
