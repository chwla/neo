"""The keyboard's settings and this profile's rebound keys.

Thin by design: parse, validate, hand off.  Every decision lives in the service.
Nothing here does authentication -- ``ProfileSessionMiddleware`` in
``app/main.py`` gates every ``/api`` path centrally, which is what stops a new
router shipping unprotected by omission, and binds the request to the signed-in
profile's database so the service reads and writes the right one without ever
being told whose it is.

Every write answers with the whole configuration rather than with what changed,
so the browser rebuilds its keymaps from one response instead of reconciling a
patch against what it thought it had.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import keybindings

router = APIRouter(prefix="/keybindings", tags=["keybindings"])


def _raise(exc: Exception) -> None:
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


class KeybindingOverride(BaseModel):
    """One command's key in one slot.  An empty sequence means unbound."""

    command_id: str = Field(min_length=1, max_length=keybindings.MAX_COMMAND_ID_LENGTH)
    keymap: Literal["primary", "alternate"]
    sequence: str = Field(max_length=keybindings.MAX_SEQUENCE_LENGTH)


class KeyboardConfig(BaseModel):
    """Everything the browser needs to build the keymap."""

    sequence_timeout_ms: int = Field(
        ge=keybindings.MIN_SEQUENCE_TIMEOUT_MS,
        le=keybindings.MAX_SEQUENCE_TIMEOUT_MS,
    )
    #: Only the commands this profile has changed.  Absence is the shipped default.
    overrides: list[KeybindingOverride]


class KeyboardConfigUpdate(BaseModel):
    """Omitted fields are left unchanged."""

    sequence_timeout_ms: int | None = Field(
        default=None,
        ge=keybindings.MIN_SEQUENCE_TIMEOUT_MS,
        le=keybindings.MAX_SEQUENCE_TIMEOUT_MS,
    )


class KeybindingWrite(BaseModel):
    """The sequence to bind.  Empty unbinds the command on purpose."""

    sequence: str = Field(max_length=keybindings.MAX_SEQUENCE_LENGTH)


@router.get("/config", response_model=KeyboardConfig)
def read_config() -> KeyboardConfig:
    return KeyboardConfig(**keybindings.config())


@router.post("/config", response_model=KeyboardConfig)
def update_config(request: KeyboardConfigUpdate) -> KeyboardConfig:
    try:
        if request.sequence_timeout_ms is not None:
            keybindings.set_sequence_timeout_ms(request.sequence_timeout_ms)
    except ValueError as exc:
        _raise(exc)
    return KeyboardConfig(**keybindings.config())


#: Validated in the path rather than in the service, so an unknown slot is a 422
#: from the schema and shows up in the OpenAPI document. The service checks it too,
#: because it is also reachable from the CLI.
Keymap = Literal["primary", "alternate"]


@router.put("/overrides/{keymap}/{command_id}", response_model=KeyboardConfig)
def set_override(keymap: Keymap, command_id: str, request: KeybindingWrite) -> KeyboardConfig:
    try:
        keybindings.set_override(command_id, keymap, request.sequence)
    except ValueError as exc:
        _raise(exc)
    return KeyboardConfig(**keybindings.config())


@router.delete("/overrides/{keymap}/{command_id}", response_model=KeyboardConfig)
def clear_override(keymap: Keymap, command_id: str) -> KeyboardConfig:
    try:
        keybindings.clear_override(command_id, keymap)
    except ValueError as exc:
        _raise(exc)
    return KeyboardConfig(**keybindings.config())


@router.delete("/overrides", response_model=KeyboardConfig)
def clear_all_overrides() -> KeyboardConfig:
    keybindings.clear_all_overrides()
    return KeyboardConfig(**keybindings.config())
