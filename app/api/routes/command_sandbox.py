from fastapi import APIRouter, HTTPException, Query

from app.services.command_sandbox import CommandSandboxService, store
from app.services.command_sandbox.types import ApprovalRequest, CommandRequest

router = APIRouter(prefix="/command-sandbox", tags=["command-sandbox"])


def service():
    return CommandSandboxService()


def fail(exc):
    raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


@router.get("/runs")
def runs(workspace_id: str | None = None, limit: int = Query(100, ge=1, le=200)):
    return {"runs": store.list_runs(workspace_id, limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    item = store.get(run_id)
    if not item:
        raise HTTPException(404, "Command run not found.")
    return {"run": item}


@router.post("/validate")
def validate(request: CommandRequest):
    return service().validate(request)


@router.post("/propose", status_code=201)
def propose(request: CommandRequest):
    return service().propose(request)


@router.post("/runs/{run_id}/approve")
def approve(run_id: str, request: ApprovalRequest):
    try:
        return service().approve(run_id, request.confirm)
    except (LookupError, ValueError) as exc:
        fail(exc)


@router.post("/runs/{run_id}/execute")
def execute(run_id: str):
    try:
        return service().execute(run_id)
    except (LookupError, ValueError) as exc:
        fail(exc)


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str):
    try:
        return service().cancel(run_id)
    except (LookupError, ValueError) as exc:
        fail(exc)
