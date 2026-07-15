"""Local profile picker, password check, and guest-session endpoints."""

from __future__ import annotations

import secrets
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services.profile_accounts import (
    authenticate,
    create_guest,
    create_profile,
    delete_guest,
    list_profiles,
)

router = APIRouter(prefix="/account-profiles", tags=["account profiles"])
SESSION_COOKIE = "neo_profile_session"
_sessions: dict[str, dict] = {}
_session_lock = Lock()


class ProfileCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=4, max_length=256)
    avatar_data: str | None = Field(default=None, max_length=3_000_000)


class UnlockRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def session_for(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _session_lock:
        return _sessions.get(token)


def _start_session(response: Response, profile: dict) -> None:
    token = secrets.token_urlsafe(32)
    with _session_lock:
        _sessions[token] = profile
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False)


@router.get("")
def profiles() -> dict:
    return {"profiles": list_profiles(), "guest_available": True}


@router.post("", status_code=201)
def register_profile(payload: ProfileCreateRequest, response: Response) -> dict:
    profile = create_profile(payload.username, payload.password, payload.avatar_data)
    _start_session(response, profile)
    return {"profile": profile}


@router.post("/guest")
def start_guest(response: Response) -> dict:
    profile = create_guest()
    _start_session(response, profile)
    return {"profile": profile}


@router.post("/{profile_id}/unlock")
def unlock_profile(profile_id: str, payload: UnlockRequest, response: Response) -> dict:
    profile = authenticate(profile_id, payload.password)
    _start_session(response, profile)
    return {"profile": profile}


@router.get("/session/current")
def current_session(request: Request) -> dict:
    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="Choose a profile to continue.")
    return {"profile": profile}


@router.post("/session/end", status_code=204)
def end_session(request: Request, response: Response) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    profile = None
    if token:
        with _session_lock:
            profile = _sessions.pop(token, None)
    if profile and profile.get("is_guest"):
        delete_guest(profile["id"])
    response.delete_cookie(SESSION_COOKIE)
    response.status_code = 204
    return response
