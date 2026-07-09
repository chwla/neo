from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query

from app.services.rules import store
from app.services.rules.importer import RepoRuleImporter
from app.services.rules.resolver import RuleResolver
from app.services.rules.types import RuleProfileCreate, RuleProfileUpdate, RuleResolveRequest

router = APIRouter(prefix="/rules", tags=["rules"])


@router.get("/profiles")
def profiles(
    scope_type: str | None = None,
    scope_id: str | None = None,
    enabled: bool | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items, total = store.list_profiles(
        scope_type=scope_type, scope_id=scope_id, enabled=enabled, limit=limit, offset=offset
    )
    return {"profiles": items, "total": total}


@router.post("/profiles", status_code=201)
def create_profile(request: RuleProfileCreate):
    if request.scope_type not in {"workspace", "global"} and not request.scope_id:
        raise HTTPException(400, "scope_id is required for this scope.")
    now = store.now_iso()
    return {
        "profile": store.insert_profile(
            {
                "id": str(uuid.uuid4()),
                **request.model_dump(),
                "source_type": "ui",
                "source_path": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    }


@router.get("/profiles/{profile_id}")
def profile(profile_id: str):
    item = store.get_profile(profile_id)
    if not item:
        raise HTTPException(404, "Rule profile not found.")
    return {"profile": item}


@router.patch("/profiles/{profile_id}")
def update_profile(profile_id: str, request: RuleProfileUpdate):
    if not store.get_profile(profile_id):
        raise HTTPException(404, "Rule profile not found.")
    updates = request.model_dump(exclude_unset=True)
    updates["updated_at"] = store.now_iso()
    return {"profile": store.update_profile(profile_id, updates)}


@router.delete("/profiles/{profile_id}")
def disable_profile(profile_id: str):
    if not store.get_profile(profile_id):
        raise HTTPException(404, "Rule profile not found.")
    return {
        "profile": store.update_profile(
            profile_id, {"enabled": False, "updated_at": store.now_iso()}
        )
    }


@router.post("/resolve")
def resolve(request: RuleResolveRequest):
    return RuleResolver().resolve(request)


@router.post("/repos/{repo_id}/import")
def import_repo(repo_id: str):
    try:
        return RepoRuleImporter().import_repo(repo_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/resolution-logs")
def logs(
    context_type: str | None = None,
    context_id: str | None = None,
    repo_id: str | None = None,
    task_id: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items, total = store.list_logs(
        context_type=context_type,
        context_id=context_id,
        repo_id=repo_id,
        task_id=task_id,
        limit=limit,
        offset=offset,
    )
    return {"resolution_logs": items, "total": total}
