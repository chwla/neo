"""Research Mode API endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.research import (
    DEPTH_CONFIG,
    DepthMode,
    JobStatus,
    StartResearchRequest,
    cancel_job,
    clear_all_jobs,
    create_job,
    get_job,
    list_jobs,
    start_job,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research", tags=["research"])


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
    )
    started = start_job(job.id)
    if not started:
        raise HTTPException(500, "Failed to start research job.")

    return StartResponse(job_id=job.id, status=job.status.value)


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
                yield _sse_event({
                    "type": "progress",
                    **progress.model_dump(),
                })

            if current.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                yield _sse_event({
                    "type": "complete",
                    "status": current.status.value,
                    "has_report": bool(current.report),
                })
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
