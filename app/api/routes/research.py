"""Research Mode API endpoints."""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.routes.accounts import session_for
from app.core.config import get_settings
from app.services.notes import Note, NotesService
from app.services.notes.service import NotesValidationError
from app.services.profile_accounts import database_identity_for_profile
from app.services.research import (
    JobStatus,
    StartResearchRequest,
    cancel_job,
    clear_all_jobs,
    create_job,
    get_job,
    list_jobs,
    start_job,
)
from app.services.research.engine import plan_preview
from app.services.research.llm_json import ResearchModelUnavailable
from app.services.research.planner import PlanningFailed
from app.services.research.types import DepthMode

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/research", tags=["research"])


class PlanPreviewRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    depth: DepthMode = DepthMode.STANDARD


@router.post("/plan")
def research_plan_preview(payload: PlanPreviewRequest):
    """The plan a run in this mode would start from, without researching anything."""
    try:
        return plan_preview(payload.question, payload.depth).model_dump(mode="json")
    except ResearchModelUnavailable as exc:
        raise HTTPException(503, f"The research model is unavailable ({exc}).") from exc
    except PlanningFailed as exc:
        raise HTTPException(422, str(exc)) from exc


class StartResponse(BaseModel):
    job_id: str
    status: str


class StatusResponse(BaseModel):
    job_id: str
    status: str
    progress_percent: int
    current_step: str
    searches_run: int = 0
    sources_reviewed: int = 0
    sources_used: int = 0
    evidence_accepted: int = 0
    open_gaps: int = 0


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
def start_research(req: StartResearchRequest, request: Request):
    if not req.query or not req.query.strip():
        raise HTTPException(400, "Query cannot be empty.")

    profile = session_for(request)
    settings = get_settings()
    use_memory = bool(
        settings.memory_enabled
        and not settings.memory_incognito
        and req.memory_enabled
        and not req.incognito
    )
    if use_memory and profile is None:
        raise HTTPException(401, "Choose an authenticated profile for memory-aware research.")
    profile_id = str(profile["id"]) if profile is not None else None
    is_guest = bool(profile and profile.get("is_guest"))

    job = create_job(
        user_query=req.query.strip(),
        depth=req.depth,
        project_id=req.project_id,
        task_id=req.task_id,
        repo_id=req.repo_id,
        owner_id=str(profile["owner_id"]) if profile is not None else None,
        database_identity=(
            database_identity_for_profile(profile_id, guest=is_guest)
            if profile_id is not None
            else None
        ),
        profile_id=profile_id,
        is_guest=is_guest,
        memory_enabled=use_memory,
        incognito=req.incognito,
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
                "depth": _mode_label(j.get("depth")),
                "status": j.get("status", "unknown"),
                "progress_percent": j.get("progress_percent", 0),
                "current_step": j.get("current_step", ""),
                "created_at": j.get("created_at", ""),
                "has_report": bool(j.get("has_report")),
            }
            for j in jobs
        ],
        "total": len(jobs),
    }


def _mode_label(value: str | None) -> str:
    """The mode a stored run belongs to today; runs saved under retired modes map onto one."""
    try:
        return DepthMode(value or "standard").value
    except ValueError:
        return str(value)


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
        searches_run=progress.searches_run,
        sources_reviewed=progress.sources_reviewed,
        sources_used=progress.sources_used,
        evidence_accepted=progress.evidence_accepted,
        open_gaps=progress.open_gaps,
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
        "sources_count": sum(1 for s in job.sources if s.used),
        "evidence_count": sum(1 for e in job.evidence if e.status == "accepted"),
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
