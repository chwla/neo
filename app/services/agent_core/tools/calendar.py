"""Adapters over ``CalendarService`` for Agent Mode.

Every mutating call is classified ``external_write``, which the permission
overlay (``agent_core/permissions.py``) maps to "ask" in every mode, including
AUTO -- Neo never creates, moves, or removes a calendar event without a human
approving it first, the same way ``deliver_changes`` never leaves the sandbox
unapproved. Reading the calendar stays ``read``, allowed everywhere.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.calendar.service import CalendarService
from app.services.calendar.types import CalendarEventCreate, CalendarEventUpdate

RECURRENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "freq": {"type": "string", "enum": ["daily", "weekly", "monthly"]},
        "interval": {"type": "integer", "minimum": 1, "maximum": 365},
        "by_weekday": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0, "maximum": 6},
            "description": "Weekly only. 0=Monday .. 6=Sunday.",
        },
        "until": {"type": "string", "description": "ISO date; mutually exclusive with count."},
        "count": {"type": "integer", "minimum": 1, "maximum": 730},
    },
    "required": ["freq"],
}

_DRAFT_PROPERTIES = {
    "title": {"type": "string"},
    "description": {"type": "string"},
    "location": {"type": "string"},
    "start_at": {"type": "string", "description": "ISO 8601 datetime, with timezone offset."},
    "end_at": {"type": "string"},
    "all_day": {"type": "boolean"},
    "timezone": {"type": "string", "description": "IANA timezone name."},
    "recurrence": RECURRENCE_SCHEMA,
    "reminder_minutes_before": {"type": "array", "items": {"type": "integer"}},
}


def _service() -> CalendarService:
    return CalendarService()


def list_calendar_events(arguments: dict, context: ToolContext) -> str:
    start = arguments.get("start")
    end = arguments.get("end")
    if not start or not end:
        raise ValueError("`start` and `end` are required ISO 8601 datetimes.")
    occurrences = _service().list_events(str(start), str(end))
    if not occurrences:
        return "No events in that range."
    lines = [f"{occ.id} — {occ.title} — {occ.occurrence_start}" for occ in occurrences]
    return "\n".join(lines)


def create_calendar_event(arguments: dict, context: ToolContext) -> str:
    try:
        payload = CalendarEventCreate.model_validate({**arguments, "source": "neo"})
    except ValidationError as exc:
        raise ValueError(f"Invalid event: {exc}") from exc
    event = _service().create_event(payload)
    return f"Created '{event.title}' at {event.start_at} (id {event.id})."


def update_calendar_event(arguments: dict, context: ToolContext) -> str:
    event_id = arguments.get("event_id")
    if not event_id:
        raise ValueError("`event_id` is required.")
    fields = {key: value for key, value in arguments.items() if key != "event_id"}
    try:
        payload = CalendarEventUpdate.model_validate(fields)
    except ValidationError as exc:
        raise ValueError(f"Invalid update: {exc}") from exc
    event = _service().update_event(str(event_id), payload)
    if event is None:
        raise ValueError(f"No event with id '{event_id}'.")
    return f"Updated '{event.title}' — now at {event.start_at}."


def delete_calendar_event(arguments: dict, context: ToolContext) -> str:
    event_id = arguments.get("event_id")
    if not event_id:
        raise ValueError("`event_id` is required.")
    event = _service().get_event(str(event_id))
    if event is None or not _service().delete_event(str(event_id)):
        raise ValueError(f"No event with id '{event_id}'.")
    return f"Removed '{event.title}'."


TOOLS = [
    AgentTool(
        name="list_calendar_events",
        description="List calendar events between two ISO 8601 datetimes, expanding recurrence.",
        parameters={
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            "required": ["start", "end"],
        },
        risk="read",
        handler=list_calendar_events,
        summary=lambda a: f"List calendar events {a.get('start')} — {a.get('end')}",
    ),
    AgentTool(
        name="create_calendar_event",
        description=(
            "Add a new event to the user's calendar. Always confirmed with the user before "
            "it is created, even in fully autonomous mode."
        ),
        parameters={
            "type": "object",
            "properties": _DRAFT_PROPERTIES,
            "required": ["title", "start_at"],
        },
        risk="external_write",
        handler=create_calendar_event,
        summary=lambda a: f"Add '{a.get('title', '(untitled)')}' — {a.get('start_at', '')}",
    ),
    AgentTool(
        name="update_calendar_event",
        description=(
            "Change an existing calendar event (found via list_calendar_events). Always "
            "confirmed with the user first, even in fully autonomous mode."
        ),
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "string"}, **_DRAFT_PROPERTIES},
            "required": ["event_id"],
        },
        risk="external_write",
        handler=update_calendar_event,
        summary=lambda a: f"Update calendar event {a.get('event_id', '')}",
    ),
    AgentTool(
        name="delete_calendar_event",
        description=(
            "Remove an event from the user's calendar. Always confirmed with the user first, "
            "even in fully autonomous mode."
        ),
        parameters={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
        risk="external_write",
        handler=delete_calendar_event,
        summary=lambda a: f"Remove calendar event {a.get('event_id', '')}",
    ),
]
