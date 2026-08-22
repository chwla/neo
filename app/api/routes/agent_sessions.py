"""HTTP surface for agent sessions."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from app.services.agent_core import events as event_types
from app.services.agent_core.service import (
    AgentCoreService,
    AgentCoreValidationError,
    ApprovalDecision,
    DeliverRequest,
    MessageCreate,
    SessionCreate,
    SessionUpdate,
)
from app.services.agent_core.workspace import WorkspaceError

router = APIRouter(prefix="/agent-sessions", tags=["agent-sessions"])

#: How long a stream waits for new events before closing. The client reconnects
#: with its last sequence number, so a close is cheap and keeps a stalled
#: connection from holding a worker thread forever.
STREAM_IDLE_TIMEOUT = 90.0
POLL_INTERVAL = 0.25


def _service() -> AgentCoreService:
    return AgentCoreService()


def _raise(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, AgentCoreValidationError | WorkspaceError | ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("", status_code=201)
def create_session(payload: SessionCreate):
    try:
        return {"session": _service().create(payload).model_dump()}
    except Exception as exc:
        _raise(exc)


@router.get("")
def list_sessions(limit: int = Query(default=25, ge=1, le=100), task_id: str | None = None):
    sessions = _service().list(limit=limit, task_id=task_id)
    return {"sessions": [item.model_dump() for item in sessions]}


@router.get("/{session_id}")
def read_session(session_id: str):
    try:
        return _service().detail(session_id)
    except Exception as exc:
        _raise(exc)


@router.get("/{session_id}/events")
def stream_events(session_id: str, after: int = Query(default=0, ge=0)):
    """Tail the durable event log as newline-delimited JSON.

    Streaming and resumption come from the same mechanism: the log is
    append-only with a monotonic sequence, so a browser that reloads reconnects
    with the last sequence it saw and misses nothing. This is what replaces the
    old 1 Hz poll of the entire run object.
    """

    service = _service()
    try:
        service.get(session_id)
    except Exception as exc:
        _raise(exc)

    def generate():
        cursor = after
        idle_since = time.monotonic()
        while True:
            batch = service.events(session_id, after=cursor)
            for event in batch:
                cursor = event["seq"]
                yield json.dumps(event, default=str) + "\n"
                if event["type"] in event_types.TERMINAL_EVENTS:
                    return
            if batch:
                idle_since = time.monotonic()
            else:
                session = service.get(session_id)
                if session.status in {"completed", "failed", "cancelled"}:
                    return
                if time.monotonic() - idle_since > STREAM_IDLE_TIMEOUT:
                    yield json.dumps({"type": "idle", "seq": cursor}) + "\n"
                    return
                time.sleep(POLL_INTERVAL)

    return StreamingResponse(generate(), media_type="application/x-ndjson")


@router.post("/{session_id}/messages")
def add_message(session_id: str, payload: MessageCreate):
    try:
        return {"session": _service().add_message(session_id, payload).model_dump()}
    except Exception as exc:
        _raise(exc)


@router.post("/{session_id}/approvals/{approval_id}")
def decide_approval(session_id: str, approval_id: str, payload: ApprovalDecision):
    try:
        return {"session": _service().decide(session_id, approval_id, payload).model_dump()}
    except Exception as exc:
        _raise(exc)


@router.post("/{session_id}/cancel")
def cancel_session(session_id: str):
    try:
        return {"session": _service().cancel(session_id).model_dump()}
    except Exception as exc:
        _raise(exc)


@router.patch("/{session_id}")
def update_session(session_id: str, payload: SessionUpdate):
    try:
        return {"session": _service().set_mode(session_id, payload).model_dump()}
    except Exception as exc:
        _raise(exc)


@router.get("/{session_id}/download")
def download(
    session_id: str,
    scope: str = Query(default="changes", pattern="^(changes|workspace)$"),
):
    """Hand the work back as a zip, for repositories Neo must not write into."""

    try:
        archive, filename = _service().download(session_id, scope)
    except Exception as exc:
        _raise(exc)
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{session_id}/deliver")
def deliver(session_id: str, payload: DeliverRequest):
    try:
        return _service().deliver(session_id, payload)
    except Exception as exc:
        _raise(exc)


@router.post("/{session_id}/export")
def export(session_id: str, target: str = Query(default="note", pattern="^(note|tasks)$")):
    try:
        return _service().export(session_id, target)
    except Exception as exc:
        _raise(exc)
