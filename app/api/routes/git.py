from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.git import store
from app.services.git.service import GitService
from app.services.git.types import (
    CheckpointCreateRequest,
    CheckpointRestoreRequest,
    GitCheckpoint,
    GitDiff,
    GitInitRequest,
    GitOperation,
    GitRepoState,
    GitStatus,
)

router = APIRouter(prefix="/git", tags=["git-checkpoints"])


def _service() -> GitService:
    return GitService()


def _raise(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        status = 404
    elif isinstance(exc, RuntimeError) and "not installed" in str(exc):
        status = 503
    else:
        status = 400
    raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/repos/{repo_id}/status")
def git_status(repo_id: str) -> GitStatus:
    try:
        return GitStatus.model_validate(_service().status(repo_id))
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)


@router.post("/repos/{repo_id}/init")
def initialize_git(repo_id: str, request: GitInitRequest) -> dict:
    try:
        state, checkpoint = _service().initialize(repo_id, request)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)
    return {
        "git_repo": GitRepoState.model_validate(state),
        "checkpoint": GitCheckpoint.model_validate(checkpoint) if checkpoint else None,
    }


@router.get("/repos/{repo_id}/diff")
def git_diff(repo_id: str, path: str | None = None) -> GitDiff:
    try:
        return GitDiff.model_validate(_service().diff(repo_id, path))
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)


@router.post("/repos/{repo_id}/checkpoints", status_code=201)
def create_checkpoint(repo_id: str, request: CheckpointCreateRequest) -> dict:
    try:
        checkpoint = _service().create_checkpoint(repo_id, request)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)
    return {"checkpoint": GitCheckpoint.model_validate(checkpoint)}


@router.get("/repos/{repo_id}/checkpoints")
def list_checkpoints(
    repo_id: str,
    task_id: str | None = None,
    patch_application_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        _service()._repo(repo_id)
    except (LookupError, ValueError) as exc:
        _raise(exc)
    items, total = store.list_checkpoints(
        repo_id=repo_id,
        task_id=task_id,
        patch_application_id=patch_application_id,
        limit=limit,
        offset=offset,
    )
    return {"checkpoints": [GitCheckpoint.model_validate(item) for item in items], "total": total}


@router.get("/checkpoints/{checkpoint_id}")
def read_checkpoint(checkpoint_id: str) -> dict:
    try:
        checkpoint, operations = _service().read_checkpoint(checkpoint_id)
    except LookupError as exc:
        _raise(exc)
    return {
        "checkpoint": GitCheckpoint.model_validate(checkpoint),
        "operations": [GitOperation.model_validate(item) for item in operations],
    }


@router.post("/checkpoints/{checkpoint_id}/restore")
def restore_checkpoint(checkpoint_id: str, request: CheckpointRestoreRequest) -> dict:
    try:
        checkpoint = _service().restore(checkpoint_id, request)
    except (LookupError, ValueError, RuntimeError) as exc:
        _raise(exc)
    return {"checkpoint": GitCheckpoint.model_validate(checkpoint)}


@router.get("/repos/{repo_id}/operations")
def list_operations(
    repo_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        _service()._repo(repo_id)
    except (LookupError, ValueError) as exc:
        _raise(exc)
    items, total = store.list_operations(repo_id, limit=limit, offset=offset)
    return {"operations": [GitOperation.model_validate(item) for item in items], "total": total}
