from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services.code_index import store
from app.services.code_index.service import CodeIndexService
from app.services.code_index.types import (
    CodeDependency,
    CodeFileSummary,
    CodeIndex,
    CodeIndexBuildRequest,
    CodeSymbol,
)
from app.services.files.types import WorkspaceFile

router = APIRouter(prefix="/code-index", tags=["code-index"])


def _service() -> CodeIndexService:
    return CodeIndexService()


def _payload(item: dict) -> dict:
    model = CodeIndex.model_validate(item)
    return {
        "index": model,
        "stats": {
            "indexed_file_count": model.indexed_file_count,
            "symbol_count": model.symbol_count,
            "dependency_count": model.dependency_count,
            "route_count": model.route_count,
        },
    }


@router.post("/repos/{repo_id}/build")
def build_index(repo_id: str, request: CodeIndexBuildRequest) -> dict:
    try:
        return _payload(_service().build(repo_id, request))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/repos/{repo_id}")
def read_index(repo_id: str) -> dict:
    try:
        return _payload(_service().get(repo_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo_id}/symbols")
def list_symbols(
    repo_id: str,
    q: str | None = None,
    symbol_type: str | None = None,
    language: str | None = None,
    relative_path: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict:
    try:
        _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    items, total = store.list_symbols(
        repo_id,
        q=q,
        symbol_type=symbol_type,
        language=language,
        relative_path=relative_path,
        limit=limit,
        offset=offset,
    )
    return {"symbols": [CodeSymbol.model_validate(item) for item in items], "total": total}


@router.get("/symbols/{symbol_id}")
def read_symbol(symbol_id: str) -> dict:
    try:
        data = _service().symbol_detail(symbol_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "symbol": CodeSymbol.model_validate(data["symbol"]),
        "file": WorkspaceFile.model_validate(data["file"]),
        "dependencies": [CodeDependency.model_validate(item) for item in data["dependencies"]],
    }


@router.get("/repos/{repo_id}/search")
def search_codebase(
    repo_id: str,
    q: str = Query(min_length=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    try:
        return {"results": _service().search(repo_id, q, limit)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo_id}/routes")
def routes(repo_id: str) -> dict:
    try:
        return {"routes": _service().routes(repo_id)}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/repos/{repo_id}/dependencies")
def dependencies(repo_id: str, relative_path: str | None = None) -> dict:
    try:
        _service().get(repo_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "dependencies": [
            CodeDependency.model_validate(item)
            for item in store.list_dependencies(repo_id, relative_path)
        ]
    }


@router.get("/repos/{repo_id}/files/{repo_file_id}/summary")
def file_summary(repo_id: str, repo_file_id: str) -> dict:
    try:
        data = _service().file_summary(repo_id, repo_file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "summary": CodeFileSummary.model_validate(data["summary"]),
        "symbols": [CodeSymbol.model_validate(item) for item in data["symbols"]],
        "dependencies": [CodeDependency.model_validate(item) for item in data["dependencies"]],
    }
