from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.services.disclosure import Labeled, content_hash, public
from app.services.github import GitHubService
from app.services.github.types import ConnectionCreate, ConnectionUpdate, PRDraftRequest

router = APIRouter(prefix="/github", tags=["github"])


def service():
    return GitHubService()


def _user_intent(connection_id, operation, **fields):
    value = {"connection_id": connection_id, "operation": operation, **fields}
    return Labeled(value, public("user_input:" + content_hash(value)))


def fail(exc):
    raise HTTPException(404 if isinstance(exc, LookupError) else 400, str(exc)) from exc


@router.get("/connections")
def connections():
    return {"connections": service().connections()}


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionCreate):
    try:
        return {"connection": service().create_connection(body.model_dump())}
    except ValueError as exc:
        fail(exc)


@router.patch("/connections/{connection_id}")
def update_connection(connection_id: str, body: ConnectionUpdate):
    try:
        return {"connection": service().update_connection(connection_id, body.model_dump())}
    except (LookupError, ValueError) as exc:
        fail(exc)


@router.delete("/connections/{connection_id}")
def disable_connection(connection_id: str):
    try:
        return {"connection": service().disable_connection(connection_id)}
    except LookupError as exc:
        fail(exc)


@router.post("/connections/{connection_id}/health")
def health(connection_id: str):
    try:
        return {
            "operation": service().health(
                connection_id,
                intent=_user_intent(connection_id, "health"),
            )
        }
    except LookupError as exc:
        fail(exc)


@router.post("/connections/{connection_id}/issues/{number}/import")
def import_issue(connection_id: str, number: int):
    try:
        return {
            "item": service().import_item(
                connection_id,
                number,
                "issue",
                intent=_user_intent(connection_id, "import_issue", number=number),
            )
        }
    except (LookupError, RuntimeError, ValueError) as exc:
        fail(exc)


@router.post("/connections/{connection_id}/pulls/{number}/import")
def import_pr(connection_id: str, number: int):
    try:
        return {
            "item": service().import_item(
                connection_id,
                number,
                "pr",
                intent=_user_intent(connection_id, "import_pr", number=number),
            )
        }
    except (LookupError, RuntimeError, ValueError) as exc:
        fail(exc)


@router.post("/items/{item_id}/create-task")
def create_task(item_id: str):
    try:
        item, task = service().create_task(item_id)
        return {"item": item, "task": task}
    except (LookupError, ValueError) as exc:
        fail(exc)


@router.get("/items")
def items():
    return {"items": service().items()}


@router.get("/items/{item_id}")
def item(item_id: str):
    try:
        return {"item": service().item(item_id)}
    except LookupError as exc:
        fail(exc)


@router.get("/operations")
def operations():
    return {"operations": service().operations()}


@router.post("/items/{item_id}/create-pr-draft")
def create_pr_draft(item_id: str, body: PRDraftRequest):
    try:
        return {"operation": service().create_pr_draft(item_id, body.model_dump())}
    except (LookupError, ValueError) as exc:
        fail(exc)
