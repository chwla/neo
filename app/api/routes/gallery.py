from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.services.gallery import store
from app.services.gallery.images import ImageError
from app.services.gallery.search import GallerySearch
from app.services.gallery.service import GalleryError, GalleryService
from app.services.gallery.types import (
    GalleryAppearance,
    GalleryEnrolRequest,
    GalleryItem,
    GalleryItemUpdate,
    GallerySearchRequest,
)

router = APIRouter(prefix="/gallery", tags=["gallery"])


def _service() -> GalleryService:
    return GalleryService()


def _raise(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


class GalleryItemResponse(BaseModel):
    item: GalleryItem
    appearances: list[GalleryAppearance] = []


class GalleryListResponse(BaseModel):
    items: list[GalleryItem]
    total: int


@router.post("/items", status_code=201)
async def enrol_image(
    file: Annotated[UploadFile | None, File()] = None,
    file_id: Annotated[str | None, Form()] = None,
    origin: Annotated[str, Form()] = "upload",
    chat_id: Annotated[int | None, Form()] = None,
    message_id: Annotated[int | None, Form()] = None,
    project_id: Annotated[str | None, Form()] = None,
) -> dict:
    """Enrol an image, either by uploading it or by naming a stored file.

    Both paths converge on the same deduplication: identical bytes resolve to the
    workspace file that already holds them, and that file already has a gallery
    item, so what is recorded is another appearance rather than a second copy.
    """

    service = _service()
    try:
        if file is not None:
            content = await file.read(get_settings().gallery_image_max_bytes + 1)
            item = service.enrol_upload(
                original_filename=file.filename or "image",
                content=content,
                mime_type=file.content_type,
                origin=origin,
                chat_id=chat_id,
                message_id=message_id,
                project_id=project_id,
            )
        elif file_id:
            item = service.enrol(
                file_id,
                origin=origin,
                chat_id=chat_id,
                message_id=message_id,
                project_id=project_id,
            )
        else:
            raise GalleryError("Provide either a file to upload or a file_id to enrol.")
    except (GalleryError, ImageError, ValueError, LookupError) as exc:
        _raise(exc)
    return {"item": GalleryItem.model_validate(item)}


@router.post("/items/enrol", status_code=201)
def enrol_existing(request: GalleryEnrolRequest) -> dict:
    """The JSON form of enrolment, for callers that already uploaded the file."""

    try:
        item = _service().enrol(
            request.file_id,
            origin=request.origin,
            chat_id=request.chat_id,
            message_id=request.message_id,
            project_id=request.project_id,
            role=request.role,
        )
    except (GalleryError, ImageError, ValueError, LookupError) as exc:
        _raise(exc)
    return {"item": GalleryItem.model_validate(item)}


@router.get("/items")
def list_items(
    q: str | None = None,
    chat_id: int | None = None,
    project_id: str | None = None,
    tag: Annotated[list[str] | None, Query()] = None,
    status: str | None = None,
    origin: str | None = None,
    pinned: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    include_deleted: bool = False,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> GalleryListResponse:
    items, total = store.list_items(
        q=q,
        chat_id=chat_id,
        project_id=project_id,
        tags=tag,
        status=status,
        origin=origin,
        pinned=pinned,
        since=since,
        until=until,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return GalleryListResponse(
        items=[GalleryItem.model_validate(item) for item in items], total=total
    )


@router.get("/items/{item_id}")
def read_item(item_id: str) -> GalleryItemResponse:
    try:
        detail = _service().detail(item_id)
    except LookupError as exc:
        _raise(exc)
    return GalleryItemResponse(
        item=GalleryItem.model_validate(detail["item"]),
        appearances=[GalleryAppearance.model_validate(a) for a in detail["appearances"]],
    )


@router.patch("/items/{item_id}")
def update_item(item_id: str, request: GalleryItemUpdate) -> dict:
    try:
        item = _service().update(item_id, request)
    except LookupError as exc:
        _raise(exc)
    return {"item": GalleryItem.model_validate(item)}


@router.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: str, purge: bool = False) -> None:
    try:
        _service().delete(item_id, purge=purge)
    except LookupError as exc:
        _raise(exc)


@router.post("/items/{item_id}/describe")
def describe_item(item_id: str, force: bool = True) -> dict:
    try:
        item = _service().request_description(item_id, force=force)
    except LookupError as exc:
        _raise(exc)
    return {"item": GalleryItem.model_validate(item)}


@router.get("/items/{item_id}/image")
def read_image(item_id: str) -> FileResponse:
    service = _service()
    try:
        item = service.get(item_id)
        path = service.image_path(item_id)
    except LookupError as exc:
        _raise(exc)
    return FileResponse(path, media_type=f"image/{(item.get('image_format') or 'png').lower()}")


@router.get("/items/{item_id}/thumbnail")
def read_thumbnail(item_id: str) -> FileResponse:
    try:
        path = _service().thumbnail_path(item_id)
    except LookupError as exc:
        _raise(exc)
    except (OSError, ImageError) as exc:
        raise HTTPException(status_code=404, detail="Thumbnail is unavailable.") from exc
    return FileResponse(
        path,
        media_type="image/webp",
        # Thumbnails are content-stable for the life of an item id, and the grid
        # asks for every one of them on each visit.
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/search")
def search(request: GallerySearchRequest) -> dict:
    outcome = GallerySearch().search(
        request.query,
        chat_id=request.chat_id,
        project_id=request.project_id,
        tags=request.tags or None,
        since=request.since,
        until=request.until,
        limit=request.limit,
    )
    return {
        "results": [
            {
                "item": GalleryItem.model_validate(entry["item"]),
                "score": entry["score"],
                "appearances": [
                    GalleryAppearance.model_validate(a) for a in entry["appearances"]
                ],
                **(
                    {"score_breakdown": entry["score_breakdown"]}
                    if request.include_score_breakdown
                    else {}
                ),
            }
            for entry in outcome["results"]
        ],
        "window": outcome.get("window"),
        "query": outcome.get("query"),
    }
