from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.test_runner import store
from app.services.test_runner.service import TestRunnerService
from app.services.test_runner.types import (
    TestCommand,
    TestCommandCreate,
    TestCommandSuggestion,
    TestCommandUpdate,
    TestRun,
    TestRunRequest,
)

router = APIRouter(prefix="/test-runner", tags=["test-runner"])


def _service() -> TestRunnerService:
    return TestRunnerService()


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404 if isinstance(exc, LookupError) else 400, detail=str(exc))


@router.get("/repos/{repo_id}/commands")
def list_commands(repo_id: str) -> dict:
    try:
        _service()._repo(repo_id)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"commands": [TestCommand.model_validate(item) for item in store.list_commands(repo_id)]}


@router.post("/repos/{repo_id}/commands", status_code=201)
def create_command(repo_id: str, request: TestCommandCreate) -> dict:
    try:
        item = _service().create_command(repo_id, request)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"command": TestCommand.model_validate(item)}


@router.patch("/commands/{command_id}")
def update_command(command_id: str, request: TestCommandUpdate) -> dict:
    try:
        item = _service().update_command(command_id, request)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"command": TestCommand.model_validate(item)}


@router.delete("/commands/{command_id}")
def disable_command(command_id: str) -> dict:
    try:
        item = _service().disable_command(command_id)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"command": TestCommand.model_validate(item)}


@router.post("/repos/{repo_id}/detect")
def detect(repo_id: str) -> dict:
    try:
        items = _service().detect(repo_id)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"suggestions": [TestCommandSuggestion.model_validate(item) for item in items]}


@router.post("/commands/{command_id}/run")
def run_command(command_id: str, request: TestRunRequest) -> dict:
    try:
        item = _service().run_command(command_id, request)
    except (LookupError, ValueError) as exc:
        raise _error(exc) from exc
    return {"run": TestRun.model_validate(item)}


@router.get("/runs")
def list_runs(
    repo_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    patch_application_id: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    items, total = store.list_runs(
        repo_id=repo_id,
        project_id=project_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        patch_application_id=patch_application_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"runs": [TestRun.model_validate(item) for item in items], "total": total}


@router.get("/runs/{run_id}")
def read_run(run_id: str) -> dict:
    item = store.get_run(run_id)
    if not item:
        raise HTTPException(status_code=404, detail="Test run not found.")
    return {"run": TestRun.model_validate(item)}
