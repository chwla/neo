from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.deps import get_store
from app.repositories.app_store import AppStore
from app.services.calendar import (
    CalendarEvent,
    CalendarEventCreate,
    CalendarEventOccurrence,
    CalendarEventUpdate,
    CalendarService,
    CalendarValidationError,
    PendingReminder,
    execute_calendar_proposal,
)
from app.services.pending_action import (
    calendar_proposal_status,
    read_calendar_proposal,
)

router = APIRouter(prefix="/calendar", tags=["calendar"])
StoreDependency = Annotated[AppStore, Depends(get_store)]


class CalendarEventResponse(BaseModel):
    event: CalendarEvent


class CalendarEventsListResponse(BaseModel):
    events: list[CalendarEventOccurrence]


class PendingRemindersResponse(BaseModel):
    reminders: list[PendingReminder]


class CalendarProposalResolutionResponse(BaseModel):
    """What the card should now show, straight from what was persisted."""

    message_id: int
    status: Literal["approved", "declined"]
    #: The stamped ``calendar_proposal`` metadata block, so the caller can
    #: swap it into the message it already holds without a second request.
    proposal: dict[str, object]
    event: CalendarEvent | None = None


def _service() -> CalendarService:
    return CalendarService()


def _proposal_block(message: object) -> dict[str, object]:
    """The stamped proposal block, ready to hand straight back to the card."""
    metadata = json.loads(message.metadata_json) if message.metadata_json else {}
    return metadata.get("calendar_proposal") or {}


@router.post("/events", response_model=CalendarEventResponse)
def create_event(payload: CalendarEventCreate):
    service = _service()
    try:
        duplicate = service.find_duplicate_event(payload)
        if duplicate is not None:
            raise HTTPException(
                409,
                f'An event titled "{duplicate.title}" already exists at that time '
                f"(id {duplicate.id}).",
            )
        event = service.create_event(payload)
    except CalendarValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    service.verify_mutation("create", event=event, proposed=payload.model_dump(mode="json"))
    return CalendarEventResponse(event=event)


@router.get("/events", response_model=CalendarEventsListResponse)
def list_events(start: str, end: str):
    try:
        events = _service().list_events(start, end)
    except CalendarValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return CalendarEventsListResponse(events=events)


@router.get("/events/{event_id}", response_model=CalendarEventResponse)
def get_event(event_id: str):
    event = _service().get_event(event_id)
    if event is None:
        raise HTTPException(404, "Event not found.")
    return CalendarEventResponse(event=event)


@router.patch("/events/{event_id}", response_model=CalendarEventResponse)
def update_event(event_id: str, payload: CalendarEventUpdate):
    service = _service()
    try:
        event = service.update_event(event_id, payload)
    except CalendarValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    if event is None:
        raise HTTPException(404, "Event not found.")
    service.verify_mutation("update", event=event, proposed=payload.model_dump(mode="json"))
    return CalendarEventResponse(event=event)


@router.delete("/events/{event_id}")
def delete_event(event_id: str):
    service = _service()
    if not service.delete_event(event_id):
        raise HTTPException(404, "Event not found.")
    service.verify_mutation("delete", deleted=True)
    return {"deleted": True}


@router.get("/reminders/pending", response_model=PendingRemindersResponse)
def pending_reminders():
    return PendingRemindersResponse(reminders=_service().pending_reminders())


@router.post("/reminders/{delivery_id}/ack")
def acknowledge_reminder(delivery_id: str):
    if not _service().acknowledge_reminder(delivery_id):
        raise HTTPException(404, "Reminder not found.")
    return {"acknowledged": True}


@router.post("/proposals/{message_id}/approve", response_model=CalendarProposalResolutionResponse)
def approve_proposal(message_id: int, store: StoreDependency):
    """Carry out a proposal, and record that it was carried out, in one request.

    Clicking Approve is the authorization -- there is no typed equivalent.
    Executing and stamping together is the point: it is what makes "the event
    exists" and "the card says it exists" unable to disagree, which is exactly
    what went wrong when the card wrote the event through the generic
    ``POST /calendar/events`` route and kept the decision in React state.
    """
    message = store.get_chat_message(message_id)
    if message is None or message.response_kind != "calendar_proposal":
        raise HTTPException(404, "Proposal not found.")
    if calendar_proposal_status(message) is not None:
        raise HTTPException(409, "That proposal has already been resolved.")
    pending = read_calendar_proposal(message)
    if pending is None:
        raise HTTPException(422, "That proposal can no longer be read.")

    outcome = execute_calendar_proposal(pending, _service())
    if outcome.status == "failed":
        # Nothing usable landed, so the proposal stays open and the card keeps
        # its buttons -- the user can retry or decline.
        raise HTTPException(422, outcome.note or "The calendar change could not be made.")

    resolved = store.resolve_calendar_proposal(
        message_id,
        status="approved",
        event_id=outcome.event_id,
        note=outcome.note,
    )
    if resolved is None:
        raise HTTPException(409, "That proposal has already been resolved.")
    store.db.commit()
    return CalendarProposalResolutionResponse(
        message_id=message_id,
        status="approved",
        proposal=_proposal_block(resolved),
        event=outcome.event,
    )


@router.post("/proposals/{message_id}/decline", response_model=CalendarProposalResolutionResponse)
def decline_proposal(message_id: int, store: StoreDependency):
    """Record that the user turned a proposal down. Writes nothing to the calendar."""
    message = store.get_chat_message(message_id)
    if message is None or message.response_kind != "calendar_proposal":
        raise HTTPException(404, "Proposal not found.")
    if calendar_proposal_status(message) is not None:
        raise HTTPException(409, "That proposal has already been resolved.")
    resolved = store.resolve_calendar_proposal(message_id, status="declined")
    if resolved is None:
        raise HTTPException(409, "That proposal has already been resolved.")
    store.db.commit()
    return CalendarProposalResolutionResponse(
        message_id=message_id,
        status="declined",
        proposal=_proposal_block(resolved),
    )
