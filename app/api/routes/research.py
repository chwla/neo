"""Research Mode API endpoints."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.notes import Note, NotesService
from app.services.notes.service import NotesValidationError
from app.services.research import (
    DEPTH_CONFIG,
    JobStatus,
    StartResearchRequest,
    cancel_job,
    clear_all_jobs,
    create_job,
    get_job,
    list_jobs,
    start_job,
)
from app.services.research_mode import ResearchModeService, ResearchPlanRequest, ResearchRunRequest
from app.services.research_mode import store as research_mode_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research", tags=["research"])


def _mode_service() -> ResearchModeService:
    return ResearchModeService()


# Enterprise Research Mode. These routes intentionally coexist with the legacy
# asynchronous research job API below so existing sessions remain readable.
@router.post("/plan")
def research_mode_plan(payload: ResearchPlanRequest):
    return _mode_service().plan(payload)


@router.post("/run")
def research_mode_run(payload: ResearchRunRequest):
    return _mode_service().run(payload)


@router.get("/runs")
def research_mode_runs(limit: int = 100):
    return {"runs": research_mode_store.list_runs(limit=min(limit, 200))}


@router.get("/runs/{run_id}")
def research_mode_detail(run_id: str):
    try:
        return _mode_service().detail(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/runs/{run_id}/evidence")
def research_mode_evidence(run_id: str):
    detail = research_mode_detail(run_id)
    return {"evidence": detail["evidence"]}


@router.get("/runs/{run_id}/claims")
def research_mode_claims(run_id: str):
    detail = research_mode_detail(run_id)
    return {"claims": detail["claims"]}


@router.get("/runs/{run_id}/conflicts")
def research_mode_conflicts(run_id: str):
    detail = research_mode_detail(run_id)
    return {"conflicts": detail["conflicts"]}


@router.get("/runs/{run_id}/report")
def research_mode_report(run_id: str):
    detail = research_mode_detail(run_id)
    if not detail["report"]:
        raise HTTPException(409, "Research report is not ready.")
    return detail["report"]


@router.post("/runs/{run_id}/continue")
def research_mode_continue(run_id: str):
    try:
        return _mode_service().continue_run(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs/{run_id}/refresh")
def research_mode_refresh(run_id: str):
    try:
        return _mode_service().refresh(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/runs/{run_id}/validate-citations")
def research_mode_validate(run_id: str):
    try:
        return _mode_service().validate_citations(run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.delete("/runs/{run_id}")
def research_mode_delete(run_id: str):
    if not research_mode_store.get_run(run_id):
        raise HTTPException(404, "Research run not found.")
    research_mode_store.delete_run(run_id)
    return {"deleted": True, "id": run_id}


class StartResponse(BaseModel):
    job_id: str
    status: str


class StatusResponse(BaseModel):
    job_id: str
    status: str
    progress_percent: int
    current_step: str
    queries_done: int = 0
    sources_found: int = 0
    sources_fetched: int = 0
    evidence_chunks: int = 0


class CancelResponse(BaseModel):
    job_id: str
    cancelled: bool


class SaveToNoteRequest(BaseModel):
    title: str | None = None
    tags: list[str] = Field(default_factory=list)


class SaveToNoteResponse(BaseModel):
    note: Note
    already_saved: bool = False


@router.post("/start", response_model=StartResponse)
def start_research(req: StartResearchRequest):
    if not req.query or not req.query.strip():
        raise HTTPException(400, "Query cannot be empty.")

    config = DEPTH_CONFIG[req.depth]
    max_sources = req.max_sources or config["max_sources"]
    max_rounds = req.max_rounds or config["max_rounds"]

    job = create_job(
        user_query=req.query.strip(),
        depth=req.depth,
        max_sources=max_sources,
        max_rounds=max_rounds,
        project_id=req.project_id,
        task_id=req.task_id,
        repo_id=req.repo_id,
    )
    started = start_job(job.id)
    if not started:
        raise HTTPException(500, "Failed to start research job.")

    return StartResponse(job_id=job.id, status=job.status.value)


@router.get("", response_model=None)
@router.get("/list", response_model=None)
def list_research_jobs(limit: int = 20, offset: int = 0):
    jobs = list_jobs(limit=min(limit, 100), offset=offset)
    return {
        "jobs": [
            {
                "id": j["id"],
                "user_query": j["user_query"],
                "depth": j.get("depth", "standard"),
                "status": j.get("status", "unknown"),
                "progress_percent": j.get("progress_percent", 0),
                "current_step": j.get("current_step", ""),
                "created_at": j.get("created_at", ""),
                "has_report": bool(j.get("report")),
            }
            for j in jobs
        ],
        "total": len(jobs),
    }


@router.delete("/clear")
def clear_research_jobs():
    count = clear_all_jobs()
    return {"cleared": count}


@router.get("/{job_id}")
def get_research_job(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    return job.model_dump()


@router.get("/{job_id}/status", response_model=StatusResponse)
def get_research_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    progress = job.current_progress()
    return StatusResponse(
        job_id=job_id,
        status=progress.status,
        progress_percent=progress.progress_percent,
        current_step=progress.current_step,
        queries_done=progress.queries_done,
        sources_found=progress.sources_found,
        sources_fetched=progress.sources_fetched,
        evidence_chunks=progress.evidence_chunks,
    )


@router.get("/{job_id}/report")
def get_research_report(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    if job.status != JobStatus.COMPLETED:
        raise HTTPException(409, f"Job is {job.status.value}, report not ready yet.")
    return {
        "job_id": job_id,
        "query": job.user_query,
        "report": job.report,
        "sources_count": sum(1 for s in job.sources if s.fetched),
        "evidence_count": len(job.evidence_chunks),
        "metadata": job.metadata,
    }


@router.post("/{job_id}/save-to-note", response_model=SaveToNoteResponse)
def save_research_to_note(job_id: str, payload: SaveToNoteRequest | None = None):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    service = NotesService()
    existing = service.find_by_source("research_report", job_id)
    if existing:
        return SaveToNoteResponse(note=existing, already_saved=True)
    try:
        note = service.save_research_report(
            job,
            title=payload.title if payload else None,
            tags=payload.tags if payload else [],
        )
    except NotesValidationError as exc:
        raise HTTPException(409, str(exc)) from exc
    return SaveToNoteResponse(note=note)


@router.post("/{job_id}/cancel", response_model=CancelResponse)
def cancel_research(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")
    if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        return CancelResponse(job_id=job_id, cancelled=False)
    result = cancel_job(job_id)
    return CancelResponse(job_id=job_id, cancelled=result)


@router.get("/{job_id}/events")
async def research_events(job_id: str):
    """SSE endpoint for real-time progress updates."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found.")

    async def event_stream():
        last_pct = -1
        last_step = ""
        while True:
            current = get_job(job_id)
            if not current:
                yield _sse_event({"type": "error", "message": "Job not found"})
                break

            if current.progress_percent != last_pct or current.current_step != last_step:
                last_pct = current.progress_percent
                last_step = current.current_step
                progress = current.current_progress()
                yield _sse_event(
                    {
                        "type": "progress",
                        **progress.model_dump(),
                    }
                )

            if current.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                yield _sse_event(
                    {
                        "type": "complete",
                        "status": current.status.value,
                        "has_report": bool(current.report),
                    }
                )
                break

            await asyncio.sleep(1.5)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
