from __future__ import annotations

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from app.services.bundles import BundleExporter, BundleImporter, store
from app.services.bundles.types import BundleExportRequest

router = APIRouter(prefix="/bundles", tags=["bundles"])


@router.post("/export", status_code=201)
def export_bundle(request: BundleExportRequest) -> dict:
    try:
        record, _ = BundleExporter().export(**request.model_dump())
        return {"bundle": record}
    except (LookupError, ValueError) as exc:
        raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


@router.get("/exports")
def list_exports() -> dict:
    return {"exports": store.list_exports()}


@router.get("/exports/{bundle_id}")
def get_export(bundle_id: str) -> dict:
    item = store.get_export(bundle_id)
    if not item:
        raise HTTPException(404, "Export bundle not found.")
    return {"bundle": item}


@router.get("/exports/{bundle_id}/download")
def download_export(bundle_id: str) -> Response:
    item = store.get_export(bundle_id)
    if not item:
        raise HTTPException(404, "Export bundle not found.")
    path = item.get("metadata", {}).get("archive_path")
    if not path:
        raise HTTPException(404, "Bundle archive is unavailable.")
    try:
        data = __import__("pathlib").Path(path).read_bytes()
    except OSError as exc:
        raise HTTPException(404, "Bundle archive is unavailable.") from exc
    return Response(
        data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{item["file_name"]}"'},
    )


async def _bytes(file: UploadFile) -> bytes:
    data = await file.read()
    if not data:
        raise HTTPException(400, "A non-empty bundle file is required.")
    return data


@router.post("/import/validate")
async def validate_import(file: UploadFile = File(...)) -> dict:  # noqa: B008
    try:
        return BundleImporter().validate(await _bytes(file))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import", status_code=201)
async def import_bundle(
    file: UploadFile = File(...),  # noqa: B008
    confirm: bool = Form(...),
    mode: str = Form("archive_only"),
) -> dict:
    if not confirm:
        raise HTTPException(400, "Archive-only import requires explicit confirmation.")
    if mode != "archive_only":
        raise HTTPException(400, "Only archive_only import is supported.")
    try:
        return {
            "bundle": BundleImporter().import_archive(
                await _bytes(file), file.filename or "bundle.zip"
            )
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/imports")
def list_imports() -> dict:
    return {"imports": store.list_imports()}


@router.get("/imports/{bundle_id}")
def get_import(bundle_id: str) -> dict:
    item = store.get_import(bundle_id)
    if not item:
        raise HTTPException(404, "Import bundle not found.")
    return {"bundle": item}
