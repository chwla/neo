"""Which SYSTEM entries this profile keeps in its sidebar.

Thin by design, like ``appearance.py``: parse, validate, hand off. Nothing here
does authentication -- ``ProfileSessionMiddleware`` in ``app/main.py`` gates
every ``/api`` path centrally, which is what stops a new router shipping
unprotected by omission, and binds the request to the signed-in profile's
database so the service reads and writes the right one without being told whose
it is.

The write answers with the whole configuration rather than with what changed,
so the browser rebuilds from one response instead of reconciling a patch.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import sidebar_nav

router = APIRouter(prefix="/sidebar-nav", tags=["sidebar"])


class SidebarNavConfig(BaseModel):
    """Everything the panel and the sidebar need."""

    #: The whole catalogue, in the order the sidebar draws it. Sent so the panel
    #: can tell an entry this server would refuse from one it has not heard of.
    items: list[str]
    hidden: list[str]


class SidebarNavConfigUpdate(BaseModel):
    """The entries to hide, in full.

    The whole set rather than a patch, because it is one screen of toggles with
    no second writer to race: the panel holds every entry on it, so sending all
    of them costs nothing and spares the server merge semantics only this caller
    would need. ``max_length`` bounds the payload; an unknown id is refused by
    the service.
    """

    hidden: list[str] = Field(default_factory=list, max_length=64)


@router.get("/config", response_model=SidebarNavConfig)
def read_config() -> SidebarNavConfig:
    return SidebarNavConfig(**sidebar_nav.config())


@router.post("/config", response_model=SidebarNavConfig)
def update_config(request: SidebarNavConfigUpdate) -> SidebarNavConfig:
    try:
        sidebar_nav.set_hidden_system_items(request.hidden)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SidebarNavConfig(**sidebar_nav.config())
