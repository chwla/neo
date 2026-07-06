from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.services.code_index.types import CodeSymbol
from app.services.symbol_awareness.service import SymbolAwarenessService
from app.services.symbol_awareness.types import CodeReference, SymbolAwarenessBuildRequest

router = APIRouter(prefix="/symbols", tags=["symbols"])


def _service() -> SymbolAwarenessService:
    return SymbolAwarenessService()


@router.post("/repos/{repo_id}/build")
def build_awareness(repo_id: str, request: SymbolAwarenessBuildRequest):
    try:
        return _service().build(repo_id, request)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        if "Build Codebase Index" in str(exc):
            return JSONResponse(
                status_code=409,
                content={"status": "not_ready", "error": str(exc)},
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/repos/{repo_id}")
def awareness_status(repo_id: str) -> dict:
    try:
        return _service().status(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo_id}/definition")
def find_definition(
    repo_id: str,
    name: str = Query(min_length=1),
    relative_path: str | None = None,
    line: int | None = Query(default=None, ge=1),
) -> dict:
    try:
        return {"definitions": _service().definitions(repo_id, name, relative_path, line)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{symbol_id}/references")
def symbol_references(
    symbol_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        items, total = _service().references_for_symbol(symbol_id, limit, offset)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "references": [CodeReference.model_validate(item) for item in items],
        "total": total,
    }


@router.get("/repos/{repo_id}/references")
def named_references(
    repo_id: str,
    name: str = Query(min_length=1),
    reference_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        items, total = _service().references_by_name(repo_id, name, reference_type, limit, offset)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "references": [CodeReference.model_validate(item) for item in items],
        "total": total,
    }


@router.get("/repos/{repo_id}/files/{repo_file_id}/document-symbols")
def file_symbols(repo_id: str, repo_file_id: str) -> dict:
    try:
        items = _service().document_symbols(repo_id, repo_file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"symbols": [CodeSymbol.model_validate(item) for item in items]}


@router.get("/repos/{repo_id}/files/{repo_file_id}/related-files")
def related_files(repo_id: str, repo_file_id: str) -> dict:
    try:
        return {"related_files": _service().related_files(repo_id, repo_file_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/{symbol_id}/context")
def symbol_context(symbol_id: str) -> dict:
    try:
        return _service().symbol_context(symbol_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
