# ruff: noqa: E501, E701
from fastapi import APIRouter, HTTPException

from app.services.web_search import store
from app.services.web_search.service import ReliableWebSearchService
from app.services.web_search.types import WebSearchPlanRequest, WebSearchRunRequest

router = APIRouter(prefix="/web-search", tags=["web-search"])


def svc():
    return ReliableWebSearchService()


@router.post("/plan")
def plan(request: WebSearchPlanRequest):
    return svc().plan(request.query, request.mode, request.freshness_required)


@router.post("/run")
def run(request: WebSearchRunRequest):
    return svc().run(request)


@router.get("/runs")
def runs():
    return {"runs": store.list_runs()}


@router.get("/runs/{run_id}")
def detail(run_id: str):
    try:
        return svc().detail(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/sources")
def sources(run_id: str):
    return {"sources": store.related("workspace_web_sources", run_id)}


@router.get("/runs/{run_id}/evidence")
def evidence(run_id: str):
    return {"evidence": store.related("workspace_web_evidence", run_id)}


@router.get("/runs/{run_id}/conflicts")
def conflicts(run_id: str):
    return {"conflicts": store.related("workspace_web_conflicts", run_id)}


@router.get("/cache")
def cache():
    return {"cache": store.cache_list()}


@router.delete("/cache/{cache_id}", status_code=204)
def delete_cache(cache_id: str):
    if not store.delete_cache(cache_id):
        raise HTTPException(404, "Cache entry not found.")


@router.post("/runs/{run_id}/refresh")
def refresh(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Web search run not found.")
    from app.services.web_search.types import WebSearchRunRequest

    return svc().run(WebSearchRunRequest(query=run["query_text"], mode=run["mode"]))
