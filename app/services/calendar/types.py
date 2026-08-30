from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RecurrenceFreq = Literal["daily", "weekly", "monthly"]
CalendarEventSource = Literal["user", "neo"]

#: The single source of truth for these bounds -- ``CalendarService`` imports
#: them rather than restating them, so a draft the LLM classifier proposes is
#: rejected by the same rule a manual create/update would be, before either
#: ever reaches the user as something to approve.
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 50_000
MAX_LOCATION_LENGTH = 200


def _non_empty_title(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Title is required.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title exceeds {MAX_TITLE_LENGTH} characters.")
    return cleaned


def _bounded_text(value: str, *, limit: int, label: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > limit:
        raise ValueError(f"{label} exceeds {limit} characters.")
    return cleaned


def _parseable_datetime(value: str, *, label: str) -> str:
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a valid ISO 8601 datetime.") from exc
    return value


class RecurrenceRule(BaseModel):
    freq: RecurrenceFreq
    interval: int = Field(default=1, ge=1, le=365)
    by_weekday: list[int] | None = None
    until: str | None = None
    count: int | None = Field(default=None, ge=1, le=730)

    @model_validator(mode="after")
    def _check_bounds(self) -> RecurrenceRule:
        if self.until is not None and self.count is not None:
            raise ValueError("A recurrence rule may set 'until' or 'count', not both.")
        if self.by_weekday is not None:
            if self.freq != "weekly":
                raise ValueError("'by_weekday' only applies to a weekly recurrence.")
            if not self.by_weekday or any(day < 0 or day > 6 for day in self.by_weekday):
                raise ValueError("'by_weekday' entries must be 0 (Mon) through 6 (Sun).")
        return self


class CalendarEvent(BaseModel):
    id: str
    title: str
    description: str = ""
    location: str = ""
    start_at: str
    end_at: str | None = None
    all_day: bool = False
    timezone: str = "UTC"
    recurrence: RecurrenceRule | None = None
    reminder_minutes_before: list[int] = Field(default_factory=list)
    source: CalendarEventSource = "user"
    created_via: dict[str, Any] | None = None
    deleted: bool = False
    created_at: str
    updated_at: str


class CalendarEventCreate(BaseModel):
    title: str
    description: str = ""
    location: str = ""
    start_at: str
    end_at: str | None = None
    all_day: bool = False
    timezone: str = "UTC"
    recurrence: RecurrenceRule | None = None
    reminder_minutes_before: list[int] = Field(default_factory=list)
    source: CalendarEventSource = "user"
    created_via: dict[str, Any] | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        return _non_empty_title(value)

    @field_validator("start_at")
    @classmethod
    def _validate_start_at(cls, value: str) -> str:
        return _parseable_datetime(value, label="start_at")

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        return _bounded_text(value, limit=MAX_DESCRIPTION_LENGTH, label="Description")

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str) -> str:
        return _bounded_text(value, limit=MAX_LOCATION_LENGTH, label="Location")

    @field_validator("end_at")
    @classmethod
    def _validate_end_at(cls, value: str | None) -> str | None:
        return _parseable_datetime(value, label="end_at") if value else value

    @model_validator(mode="after")
    def _check_end_after_start(self) -> CalendarEventCreate:
        if self.end_at and self.end_at < self.start_at:
            raise ValueError("end_at cannot be before start_at.")
        return self


class CalendarEventUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    location: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    all_day: bool | None = None
    timezone: str | None = None
    recurrence: RecurrenceRule | None = None
    reminder_minutes_before: list[int] | None = None

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        return _non_empty_title(value) if value is not None else value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str | None) -> str | None:
        return (
            _bounded_text(value, limit=MAX_DESCRIPTION_LENGTH, label="Description")
            if value is not None
            else value
        )

    @field_validator("location")
    @classmethod
    def _validate_location(cls, value: str | None) -> str | None:
        return (
            _bounded_text(value, limit=MAX_LOCATION_LENGTH, label="Location")
            if value is not None
            else value
        )

    @field_validator("start_at")
    @classmethod
    def _validate_start_at(cls, value: str | None) -> str | None:
        return _parseable_datetime(value, label="start_at") if value is not None else value

    @field_validator("end_at")
    @classmethod
    def _validate_end_at(cls, value: str | None) -> str | None:
        return _parseable_datetime(value, label="end_at") if value else value


class CalendarEventOccurrence(CalendarEvent):
    """One concrete occurrence of an event within a queried date range.

    For a non-recurring event this is the event itself; for a recurring series
    it is one expanded instance, with ``start_at``/``end_at`` overridden to the
    occurrence's own timing while the rest of the series' fields pass through.
    """

    occurrence_start: str
    occurrence_end: str | None = None
    is_recurring_instance: bool = False


class PendingReminder(BaseModel):
    delivery_id: str
    event_id: str
    event_title: str
    occurrence_start: str
    fire_at: str
