from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from app.core.config import get_settings
from app.services.files import store
from app.services.files.service import WorkspaceFilesService
from app.services.files.types import (
    ArtifactCreate,
    FileLink,
    FileLinkCreate,
    WorkspaceArtifact,
    WorkspaceFile,
)

router = APIRouter(tags=["files"])


def _service() -> WorkspaceFilesService:
    return WorkspaceFilesService()


@router.post("/files/upload")
async def upload_file(
    file: Annotated[UploadFile, File()],
    project_id: Annotated[str | None, Form()] = None,
    task_id: Annotated[str | None, Form()] = None,
    note_id: Annotated[str | None, Form()] = None,
) -> dict:
    content = await file.read(get_settings().workspace_file_max_bytes + 1)
    links = [
        (kind, value)
        for kind, value in (
            ("project", project_id),
            ("task", task_id),
            ("note", note_id),
        )
        if value
    ]
    try:
        item = _service().import_bytes(
            original_filename=file.filename or "upload",
            content=content,
            mime_type=file.content_type,
            links=links,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"file": WorkspaceFile.model_validate(item)}


@router.get("/files")
def list_files(
    q: str | None = None,
    extension: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    note_id: str | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    filters = [("project", project_id), ("task", task_id), ("note", note_id)]
    active = [(kind, value) for kind, value in filters if value]
    if len(active) > 1:
        raise HTTPException(status_code=400, detail="Filter by only one linked target at a time.")
    link_type, target_id = active[0] if active else (None, None)
    items, total = store.list_files(
        q=q,
        extension=extension,
        link_type=link_type,
        target_id=target_id,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return {"files": [WorkspaceFile.model_validate(item) for item in items], "total": total}


@router.get("/files/{file_id}")
def read_file(file_id: str) -> dict:
    try:
        item = _service().get(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "file": WorkspaceFile.model_validate(item),
        "links": [FileLink.model_validate(link) for link in store.list_links(file_id)],
    }


@router.get("/files/{file_id}/download")
def download_file(file_id: str) -> FileResponse:
    service = _service()
    try:
        item = service.get(file_id)
        path = service.download_path(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=item["display_name"], media_type=item.get("mime_type"))


@router.delete("/files/{file_id}", status_code=204)
def delete_file(file_id: str) -> None:
    try:
        _service().get(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.update_file(file_id, {"deleted": True})


@router.post("/files/{file_id}/links")
def attach_file(file_id: str, request: FileLinkCreate) -> dict:
    try:
        link = _service().attach(file_id, request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"link": FileLink.model_validate(link)}


@router.delete("/files/{file_id}/links/{link_id}", status_code=204)
def detach_file(file_id: str, link_id: str) -> None:
    if not store.delete_link(file_id, link_id):
        raise HTTPException(status_code=404, detail="File link not found.")


@router.post("/files/{file_id}/summarize")
def summarize_file(file_id: str) -> dict:
    try:
        summary = _service().summarize(file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"summary": summary}


@router.post("/artifacts", status_code=201)
def create_artifact(request: ArtifactCreate) -> dict:
    try:
        item = _service().create_artifact(request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"artifact": WorkspaceArtifact.model_validate(item)}


@router.get("/artifacts")
def list_artifacts(
    project_id: str | None = None,
    task_id: str | None = None,
    agent_run_id: str | None = None,
    artifact_type: str | None = None,
) -> dict:
    items = store.list_artifacts(
        project_id=project_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        artifact_type=artifact_type,
    )
    return {"artifacts": [WorkspaceArtifact.model_validate(item) for item in items]}


@router.get("/artifacts/{artifact_id}")
def read_artifact(artifact_id: str) -> dict:
    item = store.get_artifact(artifact_id)
    if not item:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {"artifact": WorkspaceArtifact.model_validate(item)}


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str) -> Response:
    item = store.get_artifact(artifact_id)
    if not item:
        raise HTTPException(status_code=404, detail="Artifact not found.")
    extension = "patch" if item["artifact_type"] == "patch_proposal" else "md"
    filename = f"neo-{artifact_id}.{extension}"
    return Response(
        item["content"],
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
