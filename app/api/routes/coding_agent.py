from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.coding_agent import CodingAgentService
from app.services.coding_agent.types import (
    ActionDecisionRequest,
    ActionRejectRequest,
    CodingRunCreate,
    PatchRevisionRequest,
)

router = APIRouter(prefix="/coding-agent", tags=["coding-agent"])


def _service() -> CodingAgentService:
    return CodingAgentService()


def _raise(exc: Exception) -> None:
    status = 404 if isinstance(exc, LookupError) else 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.post("/runs", status_code=201)
def start_run(request: CodingRunCreate) -> dict:
    try:
        return _service().start(request)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)


@router.get("/runs")
def list_runs(
    task_id: str | None = None,
    project_id: str | None = None,
    repo_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    runs, total = _service().list(
        task_id=task_id,
        project_id=project_id,
        repo_id=repo_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"coding_runs": runs, "total": total}


@router.get("/runs/{coding_run_id}")
def read_run(coding_run_id: str) -> dict:
    try:
        return _service().read(coding_run_id)
    except LookupError as exc:
        _raise(exc)


@router.post("/actions/{action_request_id}/approve")
def approve_action(action_request_id: str, request: ActionDecisionRequest) -> dict:
    try:
        return _service().approve(action_request_id, request.confirm, request.options)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)


@router.post("/actions/{action_request_id}/reject")
def reject_action(action_request_id: str, request: ActionRejectRequest) -> dict:
    try:
        return _service().reject(action_request_id, request.reason)
    except (LookupError, ValueError) as exc:
        _raise(exc)


@router.post("/runs/{coding_run_id}/revise-patch")
def revise_patch(coding_run_id: str, request: PatchRevisionRequest) -> dict:
    try:
        return _service().revise(coding_run_id, request.instructions)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)


@router.post("/runs/{coding_run_id}/cancel")
def cancel_run(coding_run_id: str) -> dict:
    try:
        return _service().cancel(coding_run_id)
    except LookupError as exc:
        _raise(exc)
