from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.files.types import WorkspaceFile
from app.services.repos import store
from app.services.repos.safety import in_container
from app.services.repos.service import RepoWorkspaceService
from app.services.repos.types import RepoFile, RepoRegisterRequest, RepoStats, WorkspaceRepo

router = APIRouter(prefix="/repos", tags=["repos"])


def _service() -> RepoWorkspaceService:
    return RepoWorkspaceService()


def _stats(repo: dict) -> RepoStats:
    metadata = repo.get("metadata", {})
    return RepoStats(
        file_count=repo["file_count"],
        indexed_file_count=repo["indexed_file_count"],
        total_bytes=repo["total_bytes"],
        ignored_files=metadata.get("ignored_files", 0),
        ignored_dirs=metadata.get("ignored_dirs", 0),
        unsupported_files=metadata.get("unsupported_files", 0),
    )


@router.post("/register", status_code=201)
def register_repo(request: RepoRegisterRequest) -> dict:
    try:
        repo = _service().register(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"repo": WorkspaceRepo.model_validate(repo), "stats": _stats(repo)}


@router.get("/roots")
def list_roots() -> dict:
    """Where a folder may be opened from, and which ones were opened recently.

    The browser cannot supply an absolute path -- a folder picker only yields
    paths relative to the folder chosen -- so attaching one is typed. Handing
    back the roots and the recent list turns most attachments back into a click.
    """

    from app.core.config import get_settings

    configured = [
        part for part in get_settings().workspace_live_roots.split(":") if part.strip()
    ]
    repos, _total = store.list_repos(limit=200)
    recent = [
        {"path": repo["original_path"], "name": repo["name"], "repo_id": repo["id"]}
        for repo in repos
        if repo.get("access") == store.LIVE
    ]
    return {"roots": configured, "recent": recent[:10], "containerized": in_container()}


@router.get("")
def list_repos(
    project_id: str | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    repos, total = store.list_repos(
        project_id=project_id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return {"repos": [WorkspaceRepo.model_validate(item) for item in repos], "total": total}


@router.get("/{repo_id}")
def read_repo(repo_id: str) -> dict:
    try:
        repo = _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"repo": WorkspaceRepo.model_validate(repo), "stats": _stats(repo)}


@router.get("/{repo_id}/files")
def list_repo_files(
    repo_id: str,
    q: str | None = None,
    extension: str | None = None,
    language: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    items, total = store.list_repo_files(
        repo_id,
        q=q,
        extension=extension,
        language=language,
        limit=limit,
        offset=offset,
    )
    return {"files": [RepoFile.model_validate(item) for item in items], "total": total}


@router.get("/{repo_id}/files/{repo_file_id}")
def read_repo_file(repo_id: str, repo_file_id: str) -> dict:
    try:
        mapping, file_item = _service().get_file(repo_id, repo_file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "repo_file": RepoFile.model_validate(mapping),
        "file": WorkspaceFile.model_validate(file_item),
    }


@router.delete("/{repo_id}", status_code=204)
def delete_repo(repo_id: str) -> None:
    try:
        _service().soft_delete(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
