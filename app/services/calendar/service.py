from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from dateutil.rrule import DAILY, MONTHLY, WEEKLY, rrule
from pydantic import BaseModel

import app.services.calendar.store as store
from app.services.calendar.intent import (
    CalendarDraftDecision,
    CalendarIntentClassifier,
    looks_like_a_declarative_calendar_statement,
    merge_modification_into_draft,
)
from app.services.calendar.types import (
    MAX_DESCRIPTION_LENGTH,
    MAX_LOCATION_LENGTH,
    MAX_TITLE_LENGTH,
    CalendarEvent,
    CalendarEventCreate,
    CalendarEventOccurrence,
    CalendarEventUpdate,
    PendingReminder,
    RecurrenceRule,
)
from app.services.search.live_data import resolve_timezone

if TYPE_CHECKING:
    from app.services.llm import LLMClient
    from app.services.pending_action import PendingCalendarProposal

MAX_RANGE_DAYS = 366
MAX_REMINDER_OFFSETS = 10
MAX_REMINDER_MINUTES = 43_200  # 30 days
REMINDER_LOOKAHEAD_MINUTES = 24 * 60

_FREQ = {"daily": DAILY, "weekly": WEEKLY, "monthly": MONTHLY}
_MUTATION_LOG = logging.getLogger("neo.calendar.mutation")

#: What the application actually did to the calendar during one chat turn.
#: The authority is the application's own record of the work it performed --
#: never the conversation history, never the wording of a reply, and never the
#: model's recollection. ``"failed"`` specifically means a calendar mutation
#: was requested and committed to but could not be completed, which is a
#: strictly different fact from ``"none"`` ("this turn was not about changing
#: the calendar at all") and is exactly the distinction that used to be lost.
CalendarTurnExecution = Literal["none", "create", "update", "delete", "failed"]



class CalendarValidationError(ValueError):
    pass


def _clean_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise CalendarValidationError("Title is required.")
    if len(cleaned) > MAX_TITLE_LENGTH:
        raise CalendarValidationError(f"Title exceeds {MAX_TITLE_LENGTH} characters.")
    return cleaned


def _clean_text(value: str, *, limit: int, label: str) -> str:
    cleaned = (value or "").strip()
    if len(cleaned) > limit:
        raise CalendarValidationError(f"{label} exceeds {limit} characters.")
    return cleaned


def _normalize_title(title: str) -> str:
    # Pure string ops, no regex: collapse all whitespace runs to a single
    # space and casefold for case-insensitive comparison.
    return " ".join((title or "").split()).casefold()


def _parse_dt(value: str, *, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise CalendarValidationError(f"{label} is not a valid ISO 8601 datetime.") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _validate_reminders(minutes: list[int]) -> list[int]:
    if len(minutes) > MAX_REMINDER_OFFSETS:
        raise CalendarValidationError(f"At most {MAX_REMINDER_OFFSETS} reminders are allowed.")
    cleaned = []
    for value in minutes:
        if value < 0 or value > MAX_REMINDER_MINUTES:
            raise CalendarValidationError("Reminder offsets must be between 0 and 30 days.")
        cleaned.append(int(value))
    return sorted(set(cleaned))


def _recurrence_to_json(rule: RecurrenceRule | None) -> str | None:
    return rule.model_dump_json() if rule is not None else None


def _recurrence_from_json(raw: str | None) -> RecurrenceRule | None:
    return RecurrenceRule.model_validate_json(raw) if raw else None


def _event_row_to_model(row: dict) -> CalendarEvent:
    return CalendarEvent(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        location=row["location"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        all_day=row["all_day"],
        timezone=row["timezone"],
        recurrence=_recurrence_from_json(row["recurrence_json"]),
        reminder_minutes_before=json.loads(row["reminder_minutes_before_json"] or "[]"),
        source=row["source"],
        created_via=json.loads(row["created_via_json"]) if row["created_via_json"] else None,
        deleted=row["deleted"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _rrule_for(rule: RecurrenceRule, dtstart: datetime) -> rrule:
    kwargs: dict[str, Any] = {"dtstart": dtstart, "interval": rule.interval}
    if rule.by_weekday:
        from dateutil.rrule import weekday as _weekday

        kwargs["byweekday"] = [_weekday(day) for day in rule.by_weekday]
    if rule.until:
        kwargs["until"] = _parse_dt(rule.until, label="Recurrence end date").astimezone(
            dtstart.tzinfo
        )
    elif rule.count:
        kwargs["count"] = rule.count
    else:
        # An open-ended series still needs a hard stop so expansion over a
        # bounded query window can never run away.
        kwargs["count"] = 730
    return rrule(_FREQ[rule.freq], **kwargs)


def _expand_occurrences(
    event: CalendarEvent, range_start: datetime, range_end: datetime
) -> list[CalendarEventOccurrence]:
    start_dt = _parse_dt(event.start_at, label="start_at")
    duration = None
    if event.end_at:
        duration = _parse_dt(event.end_at, label="end_at") - start_dt

    if event.recurrence is None:
        end_dt = start_dt + duration if duration is not None else start_dt
        if end_dt < range_start or start_dt > range_end:
            return []
        return [
            CalendarEventOccurrence(
                **event.model_dump(),
                occurrence_start=event.start_at,
                occurrence_end=event.end_at,
                is_recurring_instance=False,
            )
        ]

    occurrences: list[CalendarEventOccurrence] = []
    series = _rrule_for(event.recurrence, start_dt)
    for occurrence_start in series.between(range_start, range_end, inc=True):
        occurrence_end = occurrence_start + duration if duration is not None else None
        occurrences.append(
            CalendarEventOccurrence(
                **event.model_dump(),
                occurrence_start=occurrence_start.isoformat(),
                occurrence_end=occurrence_end.isoformat() if occurrence_end else None,
                is_recurring_instance=True,
            )
        )
    return occurrences


class MutationValidationResult(BaseModel):
    """Deterministic post-write check: does the tool's return value actually
    match what was proposed, or is it silently wrong (different date/time,
    timezone, or operation)? A caller must never report success to the user
    from "the call didn't raise" alone -- this is the thing that's actually
    checked. No LLM involved; no retries."""

    is_consistent: bool
    action: Literal["create", "update", "delete"]
    event_id: str | None
    mismatches: list[str]


class CalendarService:
    def create_event(self, payload: CalendarEventCreate) -> CalendarEvent:
        now = store.now_iso()
        start_dt = _parse_dt(payload.start_at, label="start_at")
        end_dt = _parse_dt(payload.end_at, label="end_at") if payload.end_at else None
        if end_dt is not None and end_dt < start_dt:
            raise CalendarValidationError("end_at cannot be before start_at.")
        row = {
            "id": str(uuid.uuid4()),
            "title": _clean_title(payload.title),
            "description": _clean_text(
                payload.description, limit=MAX_DESCRIPTION_LENGTH, label="Description"
            ),
            "location": _clean_text(payload.location, limit=MAX_LOCATION_LENGTH, label="Location"),
            "start_at": payload.start_at,
            "end_at": payload.end_at,
            "all_day": payload.all_day,
            "timezone": payload.timezone or "UTC",
            "recurrence_json": _recurrence_to_json(payload.recurrence),
            "reminder_minutes_before_json": json.dumps(
                _validate_reminders(payload.reminder_minutes_before)
            ),
            "source": payload.source,
            "created_via_json": json.dumps(payload.created_via) if payload.created_via else None,
            "created_at": now,
            "updated_at": now,
        }
        return _event_row_to_model(store.insert_event(row))

    def get_event(self, event_id: str) -> CalendarEvent | None:
        row = store.get_event(event_id)
        return _event_row_to_model(row) if row else None

    def update_event(self, event_id: str, payload: CalendarEventUpdate) -> CalendarEvent | None:
        existing = store.get_event(event_id)
        if existing is None:
            return None
        updates: dict = {}
        if payload.title is not None:
            updates["title"] = _clean_title(payload.title)
        if payload.description is not None:
            updates["description"] = _clean_text(
                payload.description, limit=MAX_DESCRIPTION_LENGTH, label="Description"
            )
        if payload.location is not None:
            updates["location"] = _clean_text(
                payload.location, limit=MAX_LOCATION_LENGTH, label="Location"
            )
        if payload.start_at is not None:
            _parse_dt(payload.start_at, label="start_at")
            updates["start_at"] = payload.start_at
        if payload.end_at is not None:
            _parse_dt(payload.end_at, label="end_at")
            updates["end_at"] = payload.end_at
        if payload.all_day is not None:
            updates["all_day"] = payload.all_day
        if payload.timezone is not None:
            updates["timezone"] = payload.timezone
        if payload.recurrence is not None:
            updates["recurrence_json"] = _recurrence_to_json(payload.recurrence)
        if payload.reminder_minutes_before is not None:
            updates["reminder_minutes_before_json"] = json.dumps(
                _validate_reminders(payload.reminder_minutes_before)
            )
        if not updates:
            return _event_row_to_model(existing)
        updated = store.update_event(event_id, updates)
        return _event_row_to_model(updated) if updated else None

    def delete_event(self, event_id: str) -> bool:
        return store.update_event(event_id, {"deleted": True}) is not None

    def list_events(self, start: str, end: str) -> list[CalendarEventOccurrence]:
        range_start = _parse_dt(start, label="start")
        range_end = _parse_dt(end, label="end")
        if range_end < range_start:
            raise CalendarValidationError("end must be after start.")
        if (range_end - range_start) > timedelta(days=MAX_RANGE_DAYS):
            raise CalendarValidationError(f"Range may not exceed {MAX_RANGE_DAYS} days.")
        rows = store.list_events_starting_before(end)
        occurrences: list[CalendarEventOccurrence] = []
        for row in rows:
            event = _event_row_to_model(row)
            occurrences.extend(_expand_occurrences(event, range_start, range_end))
        occurrences.sort(key=lambda item: item.occurrence_start)
        return occurrences

    def due_reminders(self, now: datetime | None = None) -> list[PendingReminder]:
        now = now or datetime.now(UTC)
        lookahead_end = now + timedelta(minutes=REMINDER_LOOKAHEAD_MINUTES)
        due: list[PendingReminder] = []
        for row in store.list_all_active_events():
            event = _event_row_to_model(row)
            if not event.reminder_minutes_before:
                continue
            for occurrence in _expand_occurrences(event, now, lookahead_end):
                occurrence_dt = _parse_dt(occurrence.occurrence_start, label="occurrence_start")
                for offset in event.reminder_minutes_before:
                    fire_at = occurrence_dt - timedelta(minutes=offset)
                    if fire_at > now:
                        continue
                    if store.delivery_exists(event.id, occurrence.occurrence_start, offset):
                        continue
                    delivery_id = str(uuid.uuid4())
                    store.insert_reminder_delivery(
                        {
                            "id": delivery_id,
                            "event_id": event.id,
                            "occurrence_start": occurrence.occurrence_start,
                            "offset_minutes": offset,
                            "delivered_at": store.now_iso(),
                        }
                    )
                    due.append(
                        PendingReminder(
                            delivery_id=delivery_id,
                            event_id=event.id,
                            event_title=event.title,
                            occurrence_start=occurrence.occurrence_start,
                            fire_at=fire_at.isoformat(),
                        )
                    )
        return due

    def pending_reminders(self) -> list[PendingReminder]:
        return [
            PendingReminder(
                delivery_id=row["id"],
                event_id=row["event_id"],
                event_title=row["event_title"],
                occurrence_start=row["occurrence_start"],
                fire_at=row["delivered_at"],
            )
            for row in store.list_pending_reminder_deliveries()
        ]

    def acknowledge_reminder(self, delivery_id: str) -> bool:
        return store.acknowledge_reminder_delivery(delivery_id)

    def verify_mutation(
        self,
        action: Literal["create", "update", "delete"],
        *,
        event: CalendarEvent | None = None,
        deleted: bool | None = None,
        proposed: dict[str, Any] | None = None,
    ) -> MutationValidationResult:
        """Compare what a mutation actually did against what was proposed.

        Deterministic only -- direct field comparison, no LLM, no retries.
        ``update`` only compares fields ``proposed`` actually set, mirroring
        ``update_event``'s own "only touch what changed" contract.
        """
        mismatches: list[str] = []
        event_id = event.id if event is not None else None

        if action == "delete":
            is_consistent = bool(deleted)
            if not is_consistent:
                mismatches.append("event was not deleted")
        else:
            if event is None:
                is_consistent = False
                mismatches.append("no event was returned")
            else:
                # ``CalendarEventUpdate.model_dump()`` always includes every
                # field key (``None`` for whatever the update didn't touch),
                # so presence is decided by value, not key membership -- the
                # same rule ``update_event`` itself uses to pick what to
                # write (``if payload.title is not None: ...``). For
                # ``create`` this is equally correct: required fields are
                # never ``None`` and optional ones with real defaults
                # (``all_day=False``) are still worth checking.
                proposed = proposed or {}
                if proposed.get("title") is not None and proposed.get("title") != event.title:
                    mismatches.append(
                        f"title: proposed {proposed.get('title')!r}, got {event.title!r}"
                    )
                if proposed.get("timezone") is not None and proposed.get(
                    "timezone"
                ) != event.timezone:
                    mismatches.append(
                        f"timezone: proposed {proposed.get('timezone')!r}, "
                        f"got {event.timezone!r}"
                    )
                if proposed.get("all_day") is not None and bool(
                    proposed.get("all_day")
                ) != bool(event.all_day):
                    mismatches.append(
                        f"all_day: proposed {proposed.get('all_day')!r}, got {event.all_day!r}"
                    )
                for dt_field in ("start_at", "end_at"):
                    proposed_value = proposed.get(dt_field)
                    if proposed_value is None:
                        continue
                    actual_value = getattr(event, dt_field)
                    try:
                        proposed_dt = (
                            _parse_dt(proposed_value, label=dt_field)
                            if proposed_value
                            else None
                        )
                        actual_dt = (
                            _parse_dt(actual_value, label=dt_field) if actual_value else None
                        )
                    except CalendarValidationError:
                        mismatches.append(f"{dt_field}: unparseable value")
                        continue
                    if proposed_dt != actual_dt:
                        mismatches.append(
                            f"{dt_field}: proposed {proposed_value!r}, got {actual_value!r}"
                        )
                is_consistent = not mismatches

        result = MutationValidationResult(
            is_consistent=is_consistent,
            action=action,
            event_id=event_id,
            mismatches=mismatches,
        )
        _MUTATION_LOG.warning(
            "calendar_mutation=%s",
            json.dumps(result.model_dump(mode="json"), sort_keys=True),
        )
        return result

    def find_duplicate_event(
        self,
        candidate: CalendarEventCreate,
        *,
        exclude_event_id: str | None = None,
    ) -> CalendarEvent | None:
        """Deterministic duplicate lookup for a CREATE payload.

        Identity: normalized title (whitespace-collapsed, casefolded) plus
        the exact same start instant, among currently active events in this
        profile's calendar -- profile scope is already the DB-per-profile
        connection every other calendar query uses, so there is no column to
        filter on here. If both the candidate and a same-title/same-start
        existing event specify an ``end_at``, a differing end instant means
        they are NOT the same event. No regex, no LLM, no fuzzy matching --
        literal identity only. ``exclude_event_id`` lets a caller avoid an
        event matching itself; only the CREATE path calls this today.
        """
        target_title = _normalize_title(candidate.title)
        target_start = _parse_dt(candidate.start_at, label="start_at")
        target_end = _parse_dt(candidate.end_at, label="end_at") if candidate.end_at else None
        for row in store.list_all_active_events():
            if exclude_event_id and row["id"] == exclude_event_id:
                continue
            if _normalize_title(row["title"]) != target_title:
                continue
            try:
                if _parse_dt(row["start_at"], label="start_at") != target_start:
                    continue
                if target_end is not None and row["end_at"]:
                    if _parse_dt(row["end_at"], label="end_at") != target_end:
                        continue
            except CalendarValidationError:
                # An unparseable legacy row can't be confidently matched --
                # skip it rather than block the create over unrelated data.
                continue
            return _event_row_to_model(row)
        return None


def _format_day(moment: datetime) -> str:
    """``Tue 1 Sep 2026`` -- weekday first, so the day is readable at a glance."""
    # %-d is not portable, so the day number is interpolated directly.
    return f"{moment:%a} {moment.day} {moment:%b} {moment.year}"


def format_event_when(
    start_at: str | None,
    end_at: str | None = None,
    *,
    all_day: bool = False,
) -> str:
    """When an event happens, written the way a person would read it.

    An ISO timestamp is a storage format, not an answer: "2026-09-01T16:00:00
    +05:30" makes the reader parse a date out of punctuation, and it buries
    the two things they actually asked about -- which day, and how much of it
    the event takes. This is the one place that turns a stored instant into a
    sentence, so a proposal and a schedule listing can never word the same
    moment differently.

    Falls back to the raw value on anything unparseable rather than inventing
    a time: a draft that cannot be read is a draft the user must see verbatim.
    """
    if not start_at:
        return ""
    try:
        start = _parse_dt(start_at, label="start_at")
    except CalendarValidationError:
        return start_at
    day = _format_day(start)
    if all_day:
        # An all-day event genuinely starts at midnight, but showing that
        # midnight back to the user reads as an exact start time they never
        # gave. Describe the day itself instead.
        return f"{day} (all day)"
    when = f"{day}, {start:%H:%M}"
    if not end_at:
        return when
    try:
        end = _parse_dt(end_at, label="end_at")
    except CalendarValidationError:
        return when
    if end.date() == start.date():
        # The common case, and the one worth being compact about: a block of
        # the same day reads as a span, not as two separate timestamps.
        return f"{when}-{end:%H:%M}"
    return f"{when} to {_format_day(end)}, {end:%H:%M}"


def describe_calendar_draft(action: str | None, title: str, draft: dict[str, Any] | None) -> str:
    draft = draft or {}
    when = format_event_when(
        draft.get("start_at"), draft.get("end_at"), all_day=bool(draft.get("all_day"))
    )
    verb = {"create": "add", "update": "update", "delete": "remove"}.get(action, "update")
    return f"I can {verb} **{title}**{f" on {when}" if when else ""}."


@dataclass(frozen=True)
class CalendarProposalExecution:
    """What carrying out an approved proposal actually did.

    ``status`` is the application's own record, decided by
    ``verify_mutation`` rather than by "the call didn't raise": a write that
    landed but doesn't match what was proposed is ``"failed"``, not
    ``"approved"``. ``note`` carries the sentence explaining anything the
    user needs to know -- a duplicate, a mismatch, a validation failure --
    and is ``None`` on a clean success, where the card's own wording says
    everything.
    """

    status: Literal["approved", "duplicate", "failed"]
    event_id: str | None = None
    note: str | None = None
    event: CalendarEvent | None = None


def execute_calendar_proposal(
    pending: PendingCalendarProposal, calendar: CalendarService
) -> CalendarProposalExecution:
    """Carry out a proposal the user approved, re-validating the stored draft.

    The authorization boundary: this is reached only from the approve route,
    which is reached only from a click on the proposal's own card. Nothing
    the model produced is trusted here -- the stored draft is re-validated
    through the same Pydantic models that built it, duplicates are re-checked
    against the live calendar, and the write is verified afterwards. The
    model never supplies or edits the payload that gets written.
    """
    event: CalendarEvent | None = None
    try:
        if pending.action == "create":
            payload: CalendarEventCreate | CalendarEventUpdate = CalendarEventCreate.model_validate(
                pending.draft or {}
            )
            duplicate = calendar.find_duplicate_event(payload)
            if duplicate is not None:
                return CalendarProposalExecution(
                    status="duplicate",
                    event_id=duplicate.id,
                    note=(
                        f'You already have "{duplicate.title}" scheduled at that time, '
                        "so I didn't create another one."
                    ),
                    event=duplicate,
                )
            event = calendar.create_event(payload)
            verification = calendar.verify_mutation(
                "create", event=event, proposed=payload.model_dump(mode="json")
            )
        elif pending.action == "update":
            if not pending.event_id:
                return CalendarProposalExecution(
                    status="failed",
                    note="I couldn't find that event anymore, so I didn't make the change.",
                )
            payload = CalendarEventUpdate.model_validate(pending.draft or {})
            event = calendar.update_event(pending.event_id, payload)
            if event is None:
                return CalendarProposalExecution(
                    status="failed",
                    note="That event no longer exists, so I didn't make the change.",
                )
            verification = calendar.verify_mutation(
                "update", event=event, proposed=payload.model_dump(mode="json")
            )
        else:
            if not pending.event_id:
                return CalendarProposalExecution(
                    status="failed",
                    note="I couldn't find that event anymore, so there's nothing to remove.",
                )
            deleted = calendar.delete_event(pending.event_id)
            verification = calendar.verify_mutation("delete", deleted=deleted)
    except Exception:
        return CalendarProposalExecution(
            status="failed",
            note=(
                "I couldn't make that change -- the details didn't validate. "
                "Nothing was written."
            ),
        )

    if not verification.is_consistent:
        return CalendarProposalExecution(
            status="failed",
            event_id=verification.event_id or pending.event_id,
            note=(
                "I made a change, but it doesn't match what I proposed "
                f"({'; '.join(verification.mismatches)}). Please double-check your calendar."
            ),
            event=event,
        )
    return CalendarProposalExecution(
        status="approved",
        event_id=verification.event_id or pending.event_id,
        event=event,
    )


class CalendarContextService:
    """The plain-chat integration point for calendar-shaped prompts.

    ``handle_prompt`` is the single semantic gate: it asks
    ``CalendarIntentClassifier`` exactly once whether the message is a
    calendar request at all and, if so, which of read/create/update/delete it
    is. There is no deterministic keyword/regex path into calendar behavior --
    a message that merely contains the word "calendar" (e.g. "what is a good
    calendar app?") must fail the classifier and fall through unchanged to
    normal chat. See ``app/services/chat.py`` for how this is wired into a
    chat turn -- it is only reached once the deterministic recovery/coding/
    git/tests/tasks routes have all declined the prompt, so it does not
    re-check ``chat_intent.py`` itself.

    Mutations (create/update/delete) never touch the database here: the
    classifier's draft is validated against the same Pydantic models the API
    uses, then returned as a proposal. Nothing is written until the user
    approves it through the normal ``/api/calendar/events`` endpoints, which
    validate again independently.
    """

    def __init__(self) -> None:
        self.calendar = CalendarService()
        self.classifier = CalendarIntentClassifier()
        #: What the most recent ``handle_prompt`` call did to the calendar.
        #: Every entry point resets it, so it always describes the current
        #: turn and never a previous one. Read by ``NeoChatService`` -- the
        #: same "last_*" reporting idiom ``last_web_debug`` and
        #: ``last_routing_debug`` already use, so no second authority is
        #: introduced: this reports, ``NeoChatService`` decides.
        self.last_execution: CalendarTurnExecution = "none"

    def _fail_closed(self) -> None:
        """Record that a committed calendar mutation could not be completed.

        Every ``return None`` below that follows the model *committing* to
        create/update/delete goes through here. Returning a bare ``None``
        was indistinguishable from "this message had nothing to do with the
        calendar", which is how a request that wrote nothing still reached
        free-text generation with its own wording as the only context.
        """
        self.last_execution = "failed"

    def _read_reply(self, now: datetime) -> str:
        """The user's next two weeks, grouped by day.

        A flat list of ISO timestamps makes the reader do the grouping: to see
        what Thursday looks like they have to scan every line and compare date
        prefixes. Days are the unit people actually think in, so the day is
        stated once as a heading and each entry carries only its time, which
        is the part that differs.
        """
        occurrences = self.calendar.list_events(
            now.isoformat(), (now + timedelta(days=14)).isoformat()
        )
        if not occurrences:
            return "Your calendar has nothing in the next two weeks."

        lines: list[str] = ["Upcoming events:"]
        current_day: date | None = None
        for occurrence in occurrences[:20]:
            try:
                start = _parse_dt(occurrence.occurrence_start, label="start_at")
            except CalendarValidationError:
                # Never drop an event over a value we cannot read: show it
                # verbatim under its own heading rather than hiding it.
                lines.append(f"- {occurrence.title} — {occurrence.occurrence_start}")
                current_day = None
                continue
            if start.date() != current_day:
                current_day = start.date()
                lines.append("")
                lines.append(f"**{_format_day(start)}**")
            if occurrence.all_day:
                when = "All day"
            elif occurrence.occurrence_end:
                try:
                    end = _parse_dt(occurrence.occurrence_end, label="end_at")
                    when = (
                        f"{start:%H:%M}-{end:%H:%M}"
                        if end.date() == start.date()
                        else f"{start:%H:%M} to {_format_day(end)}, {end:%H:%M}"
                    )
                except CalendarValidationError:
                    when = f"{start:%H:%M}"
            else:
                when = f"{start:%H:%M}"
            lines.append(f"- {when} — {occurrence.title}")
        if len(occurrences) > 20:
            lines.append("")
            lines.append(f"...and {len(occurrences) - 20} more.")
        return "\n".join(lines)

    def handle_prompt(
        self,
        prompt: str,
        *,
        llm: LLMClient | None,
        timezone: str | None,
        locale: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        self.last_execution = "none"
        zone = resolve_timezone(timezone, None, "UTC")
        now = datetime.now(zone)
        candidates = [
            {"id": occ.id, "title": occ.title, "start_at": occ.occurrence_start}
            for occ in self.calendar.list_events(
                (now - timedelta(days=14)).isoformat(), (now + timedelta(days=14)).isoformat()
            )
        ]
        decision = self.classifier.classify(
            prompt,
            llm=llm,
            now=now,
            timezone_label=str(zone),
            candidate_events=candidates,
        )
        # Read per pass and accumulated, never read once at the end: the
        # refinement pass resets the flag, so a first pass that committed to
        # an unresolvable mutation would otherwise be forgotten the moment a
        # second opinion was asked for.
        unresolved = self.classifier.last_unresolved_mutation
        refinement_ran = False
        if decision is None and looks_like_a_declarative_calendar_statement(prompt):
            # Bounded, one-shot: the primary pass already declined, and the
            # message independently matches a narrow "declarative statement
            # about a dated personal event" shape -- ask once more, never
            # recursively, never on messages that don't match that shape.
            refinement_ran = True
            decision = self.classifier.classify_declarative(
                prompt,
                llm=llm,
                now=now,
                timezone_label=str(zone),
                candidate_events=candidates,
            )
            unresolved = unresolved or self.classifier.last_unresolved_mutation
        if decision is None:
            # The classifier declining is normally just "not a calendar
            # message". It is *not* that when the draft named an action but
            # its date/time could not be resolved exactly -- there the model
            # did commit to a mutation and the application refused to invent
            # the missing precision, which is a failed mutation.
            if unresolved:
                self._fail_closed()
            return None
        result = self._build_response_for_decision(decision, now)
        if result is None:
            return None
        reply, metadata = result
        if refinement_ran:
            # Popped by chat.py before this metadata is spread into
            # add_chat_message / the SSE "done" event -- it never reaches the
            # persisted ChatMessage, only the routing-diagnostic log.
            metadata = {
                **metadata,
                "_calendar_refinement": {
                    "ran": True,
                    "reason": "declarative_statement_shape",
                },
            }
        return reply, metadata

    def handle_proposal_modification(
        self,
        prompt: str,
        *,
        llm: LLMClient | None,
        timezone: str | None,
        locale: str | None,
        action: str,
        event_id: str | None,
        event_title: str | None,
        draft: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any]] | None:
        """Re-propose the *pending* proposal with the user's changes applied.

        This is the modify path's replacement for a context-free
        re-classification. The proposal the user is looking at is the
        baseline and the authority for event identity: ``action`` and
        ``event_id`` are carried straight through from it and are never
        re-derived from the model, so a modification can never silently
        retarget a different event found in the calendar.

        Produces a proposal and nothing else -- the caller still has to get
        an explicit confirmation before anything is written, exactly as for
        a first-time proposal.
        """
        self.last_execution = "none"
        if not draft:
            return None
        zone = resolve_timezone(timezone, None, "UTC")
        now = datetime.now(zone)
        decision = self.classifier.classify_modification(
            prompt,
            llm=llm,
            now=now,
            baseline_action=action,
            baseline_title=event_title,
            baseline_draft=draft,
        )
        if decision is None:
            return None
        merged = merge_modification_into_draft(draft, decision.changes, now=now)
        if merged is None:
            return None
        # Identity comes from the pending proposal, never from the model.
        return self._build_response_for_decision(
            CalendarDraftDecision(
                is_calendar_action=True,
                action=action,
                confidence=decision.confidence,
                event_id=event_id,
                draft=merged,
            ),
            now,
        )

    def _build_response_for_decision(
        self, decision: CalendarDraftDecision, now: datetime
    ) -> tuple[str, dict[str, Any]] | None:
        if decision.action == "read":
            return self._read_reply(now), {"response_kind": "calendar_read"}
        if decision.clarifying_question:
            return decision.clarifying_question, {
                "response_kind": "calendar_clarification",
                "metadata": {"calendar_clarification": {"action": decision.action}},
            }
        # Every remaining fail-closed exit below sits inside a branch already
        # guarded on a mutating action -- "read" and clarifying questions have
        # returned above -- so each one is a mutation the model committed to
        # that produced nothing, and each records that rather than returning a
        # bare None.
        existing_title: str | None = None
        if decision.action in {"update", "delete"}:
            if not decision.event_id:
                self._fail_closed()
                return None
            existing = self.calendar.get_event(decision.event_id)
            if existing is None:
                # The event named in the candidate list is gone by the time the
                # model answered (deleted, or the classifier hallucinated an id) --
                # fail closed rather than show a proposal for nothing.
                self._fail_closed()
                return None
            existing_title = existing.title

        draft_payload: dict[str, Any] | None = None
        if decision.action in {"create", "update"} and decision.draft:
            try:
                if decision.action == "create":
                    validated: CalendarEventCreate | CalendarEventUpdate = (
                        CalendarEventCreate.model_validate({**decision.draft, "source": "neo"})
                    )
                else:
                    validated = CalendarEventUpdate.model_validate(decision.draft)
            except Exception:
                self._fail_closed()
                return None
            draft_payload = validated.model_dump(mode="json")

        if decision.action in {"create", "update"} and draft_payload is None:
            # The model committed to "create"/"update" but produced nothing
            # actionable -- e.g. "schedule something sometime next week",
            # where no date was ever given. Without this guard that becomes a
            # contentless proposal ("I can add **that event**. Want me to
            # create it?") which the user cannot meaningfully approve and
            # which could only fail validation on confirmation. Fail closed
            # instead, the same way an invalid draft already does.
            self._fail_closed()
            return None

        title = (draft_payload or {}).get("title") or existing_title or "that event"
        summary = describe_calendar_draft(decision.action, title, draft_payload)
        reply = f"{summary} Want me to {decision.action} it on your calendar?"
        return reply, {
            "response_kind": "calendar_proposal",
            "metadata": {
                "calendar_proposal": {
                    "action": decision.action,
                    "event_id": decision.event_id,
                    "event_title": existing_title,
                    "draft": draft_payload,
                }
            },
        }
