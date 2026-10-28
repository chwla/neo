"""Authenticated security state and browser request token."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.routes.accounts import CSRF_COOKIE, session_for
from app.services.isolation import status as isolation_status

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/isolation")
async def isolation_state() -> dict:
    return await run_in_threadpool(isolation_status)


@router.get("/csrf")
def csrf_token(request: Request) -> dict[str, str]:
    session = session_for(request)
    if session is None:
        raise HTTPException(status_code=401, detail="authenticated_profile_required")
    token = request.cookies.get(CSRF_COOKIE) or getattr(request.state, "neo_csrf_bootstrap", "")
    return {"token": token}
