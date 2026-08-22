from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.services.files.types import WorkspaceFile
from app.services.repos import store
from app.services.repos.service import RepoWorkspaceService
from app.services.repos.types import RepoFile, RepoRegisterRequest, RepoStats, WorkspaceRepo
from app.services.repos.uploads import discard, stage_upload

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


@router.post("/upload", status_code=201)
async def upload_repo(
    files: Annotated[list[UploadFile], File()],
    paths: Annotated[list[str], Form()],
    project_id: Annotated[str | None, Form()] = None,
    name: Annotated[str | None, Form()] = None,
) -> dict:
    """Register a folder uploaded from the browser as a workspace repository.

    ``paths`` carries each file's path within the chosen folder, positionally
    matched to ``files``; the browser's own file names are not used, because a
    folder upload has to preserve its directory structure.
    """

    if len(files) != len(paths):
        raise HTTPException(
            status_code=400, detail="Each uploaded file needs exactly one matching path."
        )
    entries = [(path, await item.read()) for path, item in zip(paths, files, strict=True)]
    try:
        staged = stage_upload(entries, fallback_name=(name or "").strip() or "uploaded-folder")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        repo = _service().register_upload(
            staged.root,
            name=(name or "").strip() or staged.name,
            project_id=project_id or None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        # The importer copies what it keeps into the managed workspace, so the
        # staging tree is redundant either way.
        discard(staged.root)
    return {
        "repo": WorkspaceRepo.model_validate(repo),
        "stats": _stats(repo),
        "skipped_files": staged.skipped,
    }


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
