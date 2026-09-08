from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_store
from app.models.chat import Chat
from app.repositories.app_store import AppStore
from app.services.skills import library, resolver, store
from app.services.skills.types import (
    SkillCreate,
    SkillError,
    SkillInstallFolder,
    SkillInstallGithub,
    SkillUpdate,
)

router = APIRouter(prefix="/skills", tags=["skills"])

StoreDependency = Annotated[AppStore, Depends(get_store)]


def _overrides(app_store: AppStore, chat_id: int | None) -> dict:
    """This chat's per-skill decisions, or none when asked about the library.

    A missing chat is not an error here: the panel can be opened before the
    chat exists, and answering with the library's defaults is the useful reply.
    """

    if chat_id is None:
        return {}
    chat = app_store.db.get(Chat, chat_id)
    return dict(chat.skill_overrides or {}) if chat is not None else {}


def _installed(skill_id: str) -> dict:
    skill = store.get_skill(skill_id)
    if not skill:
        raise HTTPException(404, "Skill not found.")
    return skill


def _created(skill: dict) -> dict:
    # A freshly installed skill has no per-chat opinion yet, so its effective
    # state is its default -- said explicitly so the panel can render the new
    # row without a second request.
    return {"skill": {**skill, "enabled": skill["enabled_by_default"]}}


@router.get("")
def list_skills(app_store: StoreDependency, chat_id: int | None = Query(default=None)):
    """Every installed skill, each carrying whether it is on for ``chat_id``.

    ``overrides`` is the chat's raw map, returned alongside the resolved list so
    the panel can flip one skill without restating the rest -- and so it can
    tell "this chat decided" from "this is just the default", which is what its
    reset control is for.
    """

    overrides = _overrides(app_store, chat_id)
    return {"skills": resolver.catalog(overrides), "overrides": overrides}


@router.post("", status_code=201)
def create_skill(request: SkillCreate):
    try:
        return _created(
            library.install_from_form(
                name=request.name,
                description=request.description,
                instructions=request.instructions,
                enabled_by_default=request.enabled_by_default,
            )
        )
    except SkillError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/folder", status_code=201)
def install_folder(request: SkillInstallFolder):
    try:
        return _created(
            library.install_from_folder(
                request.path, enabled_by_default=request.enabled_by_default
            )
        )
    except SkillError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/github", status_code=201)
def install_github(request: SkillInstallGithub):
    try:
        return _created(
            library.install_from_github(request.url, enabled_by_default=request.enabled_by_default)
        )
    except SkillError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.patch("/{skill_id}")
def update_skill(skill_id: str, request: SkillUpdate):
    """Rename, re-describe, or change whether new chats start with it on.

    The slug is deliberately not renamed along with the name: it is what a
    chat's overrides are keyed by, and rewriting it would silently discard every
    per-chat decision anyone had made about this skill.
    """

    _installed(skill_id)
    updates = request.model_dump(exclude_unset=True)
    if not updates:
        return {"skill": _installed(skill_id)}
    updates["updated_at"] = store.now_iso()
    return {"skill": store.update_skill(skill_id, updates)}


@router.delete("/{skill_id}", status_code=204)
def delete_skill(skill_id: str):
    library.remove(_installed(skill_id))
    return None
