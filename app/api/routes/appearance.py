"""This profile's appearance settings.

Thin by design, like ``keybindings.py``: parse, validate, hand off. Nothing
here does authentication -- ``ProfileSessionMiddleware`` in ``app/main.py``
gates every ``/api`` path centrally, which is what stops a new router shipping
unprotected by omission, and binds the request to the signed-in profile's
database so the service reads and writes the right one without being told whose
it is.

The write answers with the whole configuration rather than with what changed,
so the browser rebuilds from one response instead of reconciling a patch.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import appearance

router = APIRouter(prefix="/appearance", tags=["appearance"])


class AppearanceConfig(BaseModel):
    """Everything the browser needs to dress itself."""

    theme: str
    #: Sent so the picker can tell a theme it has a card for from one the server
    #: would refuse, rather than finding out on the write.
    available: list[str]


class AppearanceConfigUpdate(BaseModel):
    theme: str = Field(min_length=1, max_length=64)


@router.get("/config", response_model=AppearanceConfig)
def read_config() -> AppearanceConfig:
    return AppearanceConfig(**appearance.config())


@router.post("/config", response_model=AppearanceConfig)
def update_config(request: AppearanceConfigUpdate) -> AppearanceConfig:
    try:
        appearance.set_theme(request.theme)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return AppearanceConfig(**appearance.config())
