"""HTTP surface for local model setup.

Thin by design: parse, validate, hand off. Every decision lives in the service.

Nothing here does authentication. ProfileSessionMiddleware in app/main.py gates every
/api path centrally, which is what stops a new router shipping unprotected by omission
-- and it matters here, because a scan describes the user's own computer and an
unauthenticated one would report it to anybody who asked.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.local_models import catalog, installer
from app.services.local_models.service import LocalModelsService
from app.services.local_models.types import GOALS

router = APIRouter(prefix="/local-models", tags=["local-models"])


def _service() -> LocalModelsService:
    return LocalModelsService()


class InstallRequest(BaseModel):
    model_id: str = Field(min_length=1, max_length=240)


@router.get("/goals")
def goals() -> dict:
    """The choices the wizard offers, in the wording it offers them."""

    return _service().goals()


@router.get("/scan")
def scan(
    fresh: bool = Query(False, description="Look again instead of using the cached scan."),
) -> dict:
    return _service().machine(fresh=fresh)


@router.get("/recommendations")
def recommendations(
    goal: str = Query("chat", description=f"One of: {', '.join(GOALS)}."),
    limit: int = Query(40, ge=1, le=200),
    include_unfit: bool = Query(True, description="Keep models that do not fit, with a reason."),
    fresh: bool = Query(False),
) -> dict:
    try:
        return _service().recommendations(
            goal=goal, limit=limit, include_unfit=include_unfit, fresh=fresh
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/installed")
def installed() -> dict:
    return _service().installed()


@router.get("/models/{model_id:path}")
def model(model_id: str, goal: str = Query("chat")) -> dict:
    try:
        return _service().model(model_id, goal=goal)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/install")
def install(request: InstallRequest) -> StreamingResponse:
    """Download a model and switch to it, reporting progress as it goes.

    Newline-delimited JSON rather than a single response: the download is measured in
    gigabytes, and a request that returns nothing for ten minutes reads as a hung
    screen. Matches the streaming convention used by the chat and agent routes.
    """

    row = catalog.get_model(request.model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No model called '{request.model_id}'.")

    def events() -> Iterator[bytes]:
        for event in installer.install(row):
            yield (json.dumps(event) + "\n").encode()

    return StreamingResponse(events(), media_type="application/x-ndjson")


@router.post("/install/cancel")
def cancel_install(request: InstallRequest) -> dict:
    """Stop an in-progress download.

    Aborting the browser's fetch alone does not stop it -- the pull runs against
    Ollama in a server-side thread that has nothing to do with the connection to the
    browser, so it needs its own signal to stop reading and close that connection.
    """

    return {"cancelled": installer.cancel(request.model_id)}
