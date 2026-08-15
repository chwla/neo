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
    create_profile_session,
    delete_guest,
    delete_profile,
    list_profiles,
    profile_for_session,
    revoke_profile_session,
    revoke_profile_sessions,
    update_profile_account,
)

router = APIRouter(prefix="/account-profiles", tags=["account profiles"])
SESSION_COOKIE = "neo_profile_session"
PERSISTENT_SESSION_MAX_AGE = 60 * 60 * 24 * 365 * 5
_sessions: dict[str, dict] = {}
_session_lock = Lock()


class ProfileCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=48)
    password: str = Field(min_length=4, max_length=256)
    avatar_data: str | None = Field(default=None, max_length=3_000_000)


class UnlockRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class DeleteProfileRequest(UnlockRequest):
    """Password confirmation for irreversible local account deletion."""


class ProfileUpdateRequest(BaseModel):
    """Partial update of the signed-in account. Omitted fields are left unchanged."""

    username: str | None = Field(default=None, max_length=48)
    avatar_data: str | None = None
    clear_avatar: bool = False
    current_password: str | None = Field(default=None, max_length=200)
    new_password: str | None = Field(default=None, max_length=200)


def session_for(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with _session_lock:
        guest = _sessions.get(token)
    if guest is not None:
        return guest
    return profile_for_session(token)


def _start_session(response: Response, profile: dict) -> None:
    if profile.get("is_guest"):
        token = secrets.token_urlsafe(32)
        with _session_lock:
            _sessions[token] = profile
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False, path="/"
        )
        return
    token = create_profile_session(profile)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=PERSISTENT_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _end_sessions_for_profile(profile_id: str) -> None:
    with _session_lock:
        for token, profile in list(_sessions.items()):
            if profile.get("id") == profile_id:
                _sessions.pop(token, None)
    revoke_profile_sessions(profile_id)


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


@router.delete("/{profile_id}", status_code=204)
def remove_profile(
    profile_id: str,
    payload: DeleteProfileRequest,
    request: Request,
    response: Response,
) -> Response:
    delete_profile(profile_id, payload.password)
    _end_sessions_for_profile(profile_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.patch("/me")
def update_current_profile(
    payload: ProfileUpdateRequest, request: Request, response: Response
) -> dict:
    """Edit the signed-in account. Scoped to the session so one profile cannot edit another."""
    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="authenticated_profile_required")
    if profile.get("is_guest"):
        raise HTTPException(
            status_code=400, detail="Guest sessions cannot be edited. Create a profile first."
        )

    updated = update_profile_account(
        profile["id"],
        username=payload.username,
        avatar_data=payload.avatar_data,
        clear_avatar=payload.clear_avatar,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )

    if payload.new_password is not None:
        # A password change signs every other device out; this one keeps working.
        revoke_profile_sessions(profile["id"])
        token = create_profile_session(updated)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=PERSISTENT_SESSION_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
    return {"profile": updated}


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
        if profile is None:
            revoke_profile_session(token)
    if profile and profile.get("is_guest"):
        delete_guest(profile["id"])
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response
