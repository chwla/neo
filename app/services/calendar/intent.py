"""LLM-based classification of calendar-shaped chat requests.

Modelled directly on ``app.services.search.intent.SearchIntentResolver``'s
``_model_route``/``_ROUTE_DECISION_SYSTEM_PROMPT``: one JSON-only system
prompt, a regex that only *locates* the JSON blob (never classifies), and a
resolver that fails closed to ``None`` on any parse error or low confidence.
Natural language is too varied for a verb/keyword regex to classify reliably
("remind me to call the dentist" vs "remind me why we picked SQLite"), so the
decision is made by the model rather than by pattern matching.
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.services.llm import LLMMessage

if TYPE_CHECKING:
    from app.services.llm import LLMClient

CalendarAction = Literal["read", "create", "update", "delete"]

#: The actions that change the calendar, and so the only ones that can be
#: reported as having happened. ``"read"`` is deliberately absent: answering a
#: question about the calendar changes nothing, so failing to answer it is not
#: a failed mutation.
CALENDAR_MUTATING_ACTIONS = frozenset({"create", "update", "delete"})

#: 0=Monday .. 6=Sunday, matching ``RecurrenceRule.by_weekday``'s own
#: convention so the whole calendar module speaks one week-index language.
#: This is a name->index lookup for a value the model already extracted, not
#: a phrase list that decides intent -- nothing here inspects user text.
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_FIXED_DAY_OFFSETS = {
    "today": 0,
    "tomorrow": 1,
    "day_after_tomorrow": 2,
    "yesterday": -1,
}


class CalendarDateResolutionError(ValueError):
    """A ``date_expression`` was present but could not be deterministically
    resolved. Callers must fail the decision closed rather than guessing --
    silently falling back to "today" would turn an unparsed expression into
    a real event on the wrong day."""


def resolve_calendar_date_expression(expression: dict, *, now: datetime) -> date:
    """Resolve a semantic date expression against the actual current date.

    This is the project's one explicit convention for relative weekdays,
    using Monday-start calendar weeks:

    * ``today``/``tomorrow``/``day_after_tomorrow``/``yesterday``: a fixed
      offset from ``now``'s own date.
    * ``weekday`` with ``relative`` unset or ``"this"``: the upcoming
      occurrence of that weekday -- this calendar week's if it is today or
      still ahead, otherwise next week's.
    * ``weekday`` with ``relative`` ``"next"``: always the occurrence in the
      calendar week *after* the current one, even if this week's has not
      happened yet. ("next Thursday" said on a Wednesday means the Thursday
      of the following week, not tomorrow.)
    * ``explicit_date``: the literal ``YYYY-MM-DD`` the model extracted --
      no arithmetic at all.

    All arithmetic is ``datetime.date``/``timedelta``, so month, year, and
    leap-year boundaries are handled by the standard library rather than by
    hand. Raises ``CalendarDateResolutionError`` on anything it cannot
    resolve exactly; it never returns a guess.
    """
    if not isinstance(expression, dict):
        raise CalendarDateResolutionError("date_expression must be an object.")
    kind = expression.get("kind")
    today = now.date()

    if kind in _FIXED_DAY_OFFSETS:
        return today + timedelta(days=_FIXED_DAY_OFFSETS[kind])

    if kind == "weekday":
        weekday_name = expression.get("weekday")
        if not isinstance(weekday_name, str):
            raise CalendarDateResolutionError("weekday is required when kind is 'weekday'.")
        target = _WEEKDAY_INDEX.get(weekday_name.strip().casefold())
        if target is None:
            raise CalendarDateResolutionError(f"Unrecognized weekday {weekday_name!r}.")
        relative = expression.get("relative")
        if relative is not None and relative not in ("this", "next"):
            raise CalendarDateResolutionError(f"Unrecognized relative qualifier {relative!r}.")
        this_week_target = today - timedelta(days=today.weekday()) + timedelta(days=target)
        if relative == "next":
            return this_week_target + timedelta(days=7)
        if this_week_target >= today:
            return this_week_target
        return this_week_target + timedelta(days=7)

    if kind == "explicit_date":
        raw = expression.get("explicit_date")
        if not isinstance(raw, str):
            raise CalendarDateResolutionError("explicit_date is required when kind is that.")
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise CalendarDateResolutionError(f"Unparseable explicit_date {raw!r}.") from exc

    raise CalendarDateResolutionError(f"Unrecognized date_expression kind {kind!r}.")


def resolve_recurrence_until_expression(expression: dict, *, anchor: date) -> date:
    """Resolve a recurrence's semantic end boundary against a real date.

    The model reports how the user expressed the boundary; this computes what
    that means, the same split the one-off ``date_expression`` path already
    uses. ``anchor`` is the series' own start date when known, so "until the
    end of the month" means the month the series actually runs in rather than
    whichever month it happens to be described in.

    Raises ``CalendarDateResolutionError`` on anything it cannot resolve
    exactly -- never a guess, since a wrong end date silently truncates or
    extends a whole series.
    """
    if not isinstance(expression, dict):
        raise CalendarDateResolutionError("until_expression must be an object.")
    kind = expression.get("kind")

    if kind == "end_of_year":
        return date(anchor.year, 12, 31)
    if kind == "end_of_month":
        return date(anchor.year, anchor.month, monthrange(anchor.year, anchor.month)[1])
    if kind == "explicit_date":
        raw = expression.get("explicit_date")
        if not isinstance(raw, str):
            raise CalendarDateResolutionError("explicit_date is required when kind is that.")
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise CalendarDateResolutionError(f"Unparseable explicit_date {raw!r}.") from exc

    raise CalendarDateResolutionError(f"Unrecognized until_expression kind {kind!r}.")


def _normalize_by_weekday(value: object) -> list[int] | None:
    """Turn weekday names into the 0=Mon..6=Sun indexes storage expects.

    Names are the model's side of the contract; the index mapping is this
    module's single canonical one (``_WEEKDAY_INDEX``), shared with one-off
    weekday resolution. Existing integer input is still accepted so drafts
    that already speak indexes keep working unchanged.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise CalendarDateResolutionError("by_weekday must be a non-empty list.")
    days: list[int] = []
    for entry in value:
        if isinstance(entry, bool):
            raise CalendarDateResolutionError(f"Invalid weekday {entry!r}.")
        if isinstance(entry, int):
            if not 0 <= entry <= 6:
                raise CalendarDateResolutionError(f"Weekday index {entry} out of range.")
            days.append(entry)
        elif isinstance(entry, str):
            index = _WEEKDAY_INDEX.get(entry.strip().casefold())
            if index is None:
                raise CalendarDateResolutionError(f"Unrecognized weekday {entry!r}.")
            days.append(index)
        else:
            raise CalendarDateResolutionError(f"Invalid weekday {entry!r}.")
    return sorted(set(days))


#: Reverse of ``_WEEKDAY_INDEX``, so a derived series start can be expressed
#: through the same single weekday resolver rather than a second convention.
_WEEKDAY_NAMES = {index: name for name, index in _WEEKDAY_INDEX.items()}


def _series_start_date(
    expression: object, recurrence: object, *, now: datetime
) -> date:
    """The date a draft starts on, deriving it from the recurrence if needed.

    A series repeating on several weekdays has no single weekday in its
    ``date_expression`` -- the first occurrence is simply the soonest of the
    days it repeats on, which is mechanically computable. Deriving it here
    keeps that off the model, which otherwise has to decide which of "every
    Monday and Wednesday" the series starts on.
    """
    if (
        isinstance(expression, dict)
        and expression.get("kind") == "weekday"
        and not isinstance(expression.get("weekday"), str)
        and isinstance(recurrence, dict)
    ):
        days = _normalize_by_weekday(recurrence.get("by_weekday"))
        if days:
            return min(
                resolve_calendar_date_expression(
                    {"kind": "weekday", "weekday": _WEEKDAY_NAMES[day]}, now=now
                )
                for day in days
            )
    if not isinstance(expression, dict):
        raise CalendarDateResolutionError("date_expression must be an object.")
    return resolve_calendar_date_expression(expression, now=now)


def _resolve_recurrence(
    recurrence: dict, *, now: datetime, start_date: date | None
) -> dict:
    """Normalize a recurrence rule into the shape storage validates.

    Raises ``CalendarDateResolutionError`` so the caller drops the whole
    decision: a recurring event whose end boundary could not be resolved must
    not quietly become an open-ended series or a one-off.
    """
    resolved = dict(recurrence)

    normalized = _normalize_by_weekday(resolved.get("by_weekday"))
    if normalized is not None:
        resolved["by_weekday"] = normalized

    expression = resolved.pop("until_expression", None)
    if expression is not None:
        if resolved.get("until") is not None:
            raise CalendarDateResolutionError(
                "A recurrence may express its end once, not as both until and "
                "until_expression."
            )
        anchor = start_date or now.date()
        until = resolve_recurrence_until_expression(expression, anchor=anchor)
        if start_date is not None and until < start_date:
            raise CalendarDateResolutionError("A recurrence cannot end before it starts.")
        resolved["until"] = until.isoformat()
    return resolved


def _parse_clock_time(value: object) -> time:
    """Parse an ``HH:MM`` clock time the model extracted. Raises rather than
    defaulting -- an unusable time is never quietly turned into midnight."""
    if not isinstance(value, str):
        raise CalendarDateResolutionError("A clock time is required.")
    try:
        return time.fromisoformat(value.strip())
    except ValueError as exc:
        raise CalendarDateResolutionError(f"Unparseable clock time {value!r}.") from exc


#: A block of the day, in minutes. Anything longer is almost certainly the
#: model having misread a date as a duration, so it fails closed instead.
_MAX_DURATION_MINUTES = 60 * 24 * 7


def _parse_duration_minutes(value: object) -> int:
    """How long an event lasts, when the user said a length rather than an end.

    Kept as a number the *application* adds, for the same reason dates are:
    "4pm for 90 minutes" -> 17:30 is arithmetic, and a model doing arithmetic
    silently is how an event ends up in the wrong place. The model reports the
    length it was told; the clock work happens here.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalendarDateResolutionError(f"Unusable duration {value!r}.")
    minutes = int(value)
    if minutes <= 0 or minutes > _MAX_DURATION_MINUTES:
        raise CalendarDateResolutionError(f"Out-of-range duration {value!r}.")
    return minutes


def _end_of_block(
    start: datetime,
    *,
    end_time_of_day: object = None,
    duration_minutes: object = None,
) -> datetime | None:
    """The instant a timed block ends, from either an end time or a length.

    A span shares its start's date -- the V1 contract (see
    ``CalendarEventCreate``'s start/end check, and the overnight tests). An
    end clock time before the start therefore stays on the start's date and
    lands end-before-start, which storage refuses: the unsupported overnight
    case can become a *refused* event but never a wrong one. A duration that
    would run past midnight is refused here for the same reason, so it is not
    a back door around the rule that the clock-time form obeys.
    """
    if end_time_of_day is not None:
        return datetime.combine(
            start.date(), _parse_clock_time(end_time_of_day), tzinfo=start.tzinfo
        )
    if duration_minutes is not None:
        end = start + timedelta(minutes=_parse_duration_minutes(duration_minutes))
        if end.date() != start.date():
            raise CalendarDateResolutionError(
                "A duration may not run past midnight; give an end time instead."
            )
        return end
    return None


def _resolve_draft_dates(draft: dict, *, now: datetime) -> dict | None:
    """Turn a draft's semantic date/time fields into absolute ISO datetimes.

    Returns ``None`` -- fail closed, so the caller drops the whole decision
    -- whenever a ``date_expression`` was clearly intended but cannot be
    resolved exactly. A draft with no ``date_expression`` at all is a
    different case entirely: it is returned untouched, preserving the
    pre-existing contract where ``start_at`` arrives ready-made.
    """
    try:
        resolved = {
            key: value
            for key, value in draft.items()
            if key
            not in ("date_expression", "time_of_day", "end_time_of_day", "duration_minutes")
        }
        if "date_expression" in draft:
            resolved_date = _series_start_date(
                draft["date_expression"], draft.get("recurrence"), now=now
            )
            all_day = bool(draft.get("all_day"))
            start_time = time(0, 0) if all_day else _parse_clock_time(draft.get("time_of_day"))
            start = datetime.combine(resolved_date, start_time, tzinfo=now.tzinfo)
            resolved["start_at"] = start.isoformat()
            end = (
                None
                if all_day
                else _end_of_block(
                    start,
                    end_time_of_day=draft.get("end_time_of_day"),
                    duration_minutes=draft.get("duration_minutes"),
                )
            )
            resolved["end_at"] = end.isoformat() if end is not None else None

        # Recurrence is resolved independently of the start date: a draft may
        # carry a ready-made start_at and still express its end boundary
        # semantically.
        recurrence = resolved.get("recurrence")
        if isinstance(recurrence, dict):
            start_date: date | None = None
            raw_start = resolved.get("start_at")
            if isinstance(raw_start, str) and raw_start:
                start_date = datetime.fromisoformat(raw_start).date()
            resolved["recurrence"] = _resolve_recurrence(
                recurrence, now=now, start_date=start_date
            )
    except (CalendarDateResolutionError, TypeError, ValueError):
        return None
    return resolved


def merge_modification_into_draft(
    draft: dict, changes: dict, *, now: datetime
) -> dict | None:
    """Apply an explicitly-requested change set on top of a validated draft.

    The already-validated pending draft is the baseline and the sole source
    of truth for event identity: every field the user did not explicitly ask
    to change is carried over untouched. The model's output is treated as a
    set of *requested changes*, never as a replacement draft -- so a model
    that echoes back a different title (or an unrelated event's details)
    cannot overwrite what the user is actually looking at.

    Dates are never computed by the model: a changed date arrives as a
    ``date_expression`` and goes through the same deterministic
    ``resolve_calendar_date_expression`` the primary path uses. An unchanged
    date is taken from the baseline's own ``start_at``.

    Returns ``None`` (fail closed) if the merge cannot be completed exactly.
    """
    merged = dict(draft)
    try:
        base_start: datetime | None = None
        raw_start = draft.get("start_at")
        if isinstance(raw_start, str) and raw_start:
            base_start = datetime.fromisoformat(raw_start)

        wants_date = "date_expression" in changes
        wants_time = "time_of_day" in changes
        if wants_date or wants_time or base_start is not None:
            if base_start is None and not (wants_date and wants_time):
                # No baseline timestamp to build on and the reply did not
                # supply a complete one -- never invent the missing half.
                return None
            # Prefer the active calendar timezone over the baseline's stored
            # offset. ``datetime.fromisoformat`` yields a *fixed* offset, so
            # reusing it freezes the offset that applied on the draft's
            # original date: moving an event across a DST boundary would then
            # keep e.g. -07:00 and silently shift the user's intended local
            # wall-clock time by an hour. ``now.tzinfo`` is the real zone, so
            # it recomputes the correct offset for whatever date we land on.
            zone = now.tzinfo or (base_start.tzinfo if base_start is not None else None)
            new_date = (
                resolve_calendar_date_expression(changes["date_expression"], now=now)
                if wants_date
                else base_start.date()
            )
            new_time = (
                _parse_clock_time(changes["time_of_day"])
                if wants_time
                else base_start.timetz().replace(tzinfo=None)
            )
            new_start = datetime.combine(new_date, new_time, tzinfo=zone)
            merged["start_at"] = new_start.isoformat()
            base_end = (
                datetime.fromisoformat(draft["end_at"])
                if isinstance(draft.get("end_at"), str) and draft["end_at"]
                else None
            )
            if "end_time_of_day" in changes or "duration_minutes" in changes:
                new_end = _end_of_block(
                    new_start,
                    end_time_of_day=changes.get("end_time_of_day"),
                    duration_minutes=changes.get("duration_minutes"),
                )
                merged["end_at"] = new_end.isoformat() if new_end is not None else None
            elif base_end is not None and base_start is not None:
                # Neither boundary was named, so the block keeps its length.
                # Pinning the old end clock time instead would silently
                # reshape a 4-6pm block into 5-6pm the moment the user moved
                # the start -- shortening something they only asked to move.
                merged["end_at"] = (new_start + (base_end - base_start)).isoformat()
            elif base_end is not None and wants_date:
                # No baseline start to measure a length against: carry the end
                # clock time onto the new date rather than stranding it.
                merged["end_at"] = datetime.combine(
                    new_date, base_end.timetz().replace(tzinfo=None), tzinfo=zone
                ).isoformat()
    except (CalendarDateResolutionError, TypeError, ValueError):
        return None

    for field in ("title", "location", "all_day"):
        if changes.get(field) is not None:
            merged[field] = changes[field]
    return merged


_SYSTEM_PROMPT = """You are Neo's conservative calendar-request classifier.

Decide whether the user's message is actually asking you to do something with
THEIR OWN calendar: READ it (a schedule/availability question), or CREATE,
UPDATE, or DELETE an event on it. Do not answer the message. This is a
semantic judgment, not a keyword match -- the word "calendar" appearing
somewhere in the message is not sufficient. A general question about calendar
apps/software, an offhand remark about being busy, or any other message that
merely mentions calendars/schedules without requesting a lookup or change to
the user's own calendar is NOT a calendar action.

Return exactly one JSON object with no Markdown or extra text:
{
  "is_calendar_action": true|false,
  "action": "read"|"create"|"update"|"delete"|null,
  "confidence": 0.0-1.0,
  "event_id": "<id from the candidate list below>"|null,
  "draft": {
    "title": "<short title>",
    "date_expression": {
      "kind": "today"|"tomorrow"|"day_after_tomorrow"|"yesterday"|"weekday"|"explicit_date",
      "weekday": "monday".."sunday"|null,
      "relative": "this"|"next"|null,
      "explicit_date": "YYYY-MM-DD"|null
    },
    "time_of_day": "HH:MM"|null,
    "end_time_of_day": "HH:MM"|null,
    "duration_minutes": <int>|null,
    "all_day": true|false,
    "location": "<or empty string>",
    "recurrence": {"freq": "daily"|"weekly"|"monthly", "interval": 1,
                    "by_weekday": ["monday".."sunday"]|null,
                    "until_expression": {"kind": "end_of_year"|"end_of_month"|
                      "explicit_date", "explicit_date": "YYYY-MM-DD"}|null,
                    "count": <int>|null} | null
  } | null,
  "clarifying_question": "<a short question>"|null
}

Keep the reply as SHORT as possible -- a long reply can be cut off before it
finishes, which throws the whole answer away. Emit compact JSON on one line
(no pretty-printing, no indentation) and omit:
- any key whose value would be null
- "all_day" unless it is true
- "location" unless there is a real location
- "interval" unless it is not 1
- "is_calendar_action" when you are returning an "action" (it is implied);
  send it only to say false
Only "action" is always required.

IMPORTANT -- you do NOT calculate dates. Report what the user *said*, and the
application computes the actual calendar date from it:
- If the user named a weekday, set "kind" to "weekday", "weekday" to that day's
  name, and "relative" to "next" only if they explicitly said "next", otherwise
  null. Never work out which date that weekday falls on.
- Use "today"/"tomorrow"/"day_after_tomorrow"/"yesterday" ONLY when the user
  used that wording themselves. A named weekday stays "weekday" even when you
  can see which offset it lands on -- that conversion is date arithmetic.
- Use "explicit_date" only when a specific calendar date is already known --
  the user stated one, or you are changing only the time of a candidate event
  listed below and are copying that event's existing date verbatim.
- "time_of_day" and "end_time_of_day" are 24-hour "HH:MM" clock times
  ("3pm" -> "15:00"). Set "time_of_day" to null only when "all_day" is true.
- When the user gives an END as a clock time -- "4pm to 6pm", "2-3pm",
  "from 9 until 5" -- set "end_time_of_day" to that time. When they give a
  LENGTH instead -- "for an hour", "30 min", "for 90 minutes" -- set
  "duration_minutes" to the number of minutes and leave "end_time_of_day"
  null. Never both, and never work the one out from the other: that is
  arithmetic, and the application does it. Omit both when the user named
  only a start.

Rules:
- "read" is for a question about the user's own schedule or events (e.g.
  "what's on my calendar Friday", "what events do I have tomorrow", "am I
  free Thursday afternoon"). Leave "event_id" and "draft" null.
- "create" needs a "draft" with at least "title", "date_expression", and
  "time_of_day" (unless "all_day"). Leave "event_id" null.
- "update" and "delete" need "event_id" set to one of the candidate events
  listed below. If no candidate is a confident match, keep the "action",
  leave "event_id" null, and put a short clarifying question in
  "clarifying_question" instead of guessing.
- "by_weekday" only applies to a weekly recurrence. Name the days
  ("monday", "wednesday"); never convert them to numbers yourself.
- A recurrence end is a date you must NOT calculate either. Report it as
  "until_expression" ("until the end of the year" -> {"kind":
  "end_of_year"}). Use "count" instead when the user gave a number of
  occurrences. Never set both.
- The repeating days belong in "recurrence.by_weekday" only. Never put
  "by_weekday" inside "date_expression". For a series repeating on several
  days you may leave "date_expression" as {"kind": "weekday"} with no
  "weekday" -- the application starts the series on the soonest of the
  repeating days.
- Default "confidence" low (below 0.6) for anything ambiguous. When
  "is_calendar_action" is false, every other field must be null.
- "Is this a calendar action?" and "can I fill in the draft?" are two SEPARATE
  questions. Decide the first from what the user wants, and only that. Asking
  you to put something on the calendar is a "create" whether or not they ever
  said when; asking you to change or drop something on it is an "update" or a
  "delete" whether or not you can tell which event they mean. Missing, vague
  or unusable details NEVER turn a calendar request into
  "is_calendar_action": false.
- Never invent a date or a time. When the user did not name a definite day, or
  did not name a definite clock time, or was only approximate about either,
  keep the "action", leave "draft" null, and put a short
  "clarifying_question" in it asking for the exact day and time. A question
  is always better than a guess, and always better than dropping the request.
- Set "is_calendar_action" to false only when the message is not asking you to
  read or change the user's own calendar at all.

Examples:
CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: remind me to call the dentist Friday at 3pm
JSON: {"action":"create","confidence":0.92,"draft":{"title":"Call the dentist","date_express\
ion":{"kind":"weekday","weekday":"friday"},"time_of_day":"15:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: book a haircut next Monday at 10:30am
JSON: {"action":"create","confidence":0.9,"draft":{"title":"Haircut","date_expression":{"kin\
d":"weekday","weekday":"monday","relative":"next"},"time_of_day":"10:30"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: physio Thursday at 3pm
JSON: {"action":"create","confidence":0.9,"draft":{"title":"Physio","date_expression":{"ki\
nd":"weekday","weekday":"thursday"},"time_of_day":"15:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: lunch with Sam tomorrow at noon
JSON: {"action":"create","confidence":0.9,"draft":{"title":"Lunch with Sam","date_expression\
":{"kind":"tomorrow"},"time_of_day":"12:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: work on neo on tuesday 4pm to 6pm
JSON: {"action":"create","confidence":0.92,"draft":{"title":"Work on neo","date_expression":\
{"kind":"weekday","weekday":"tuesday"},"time_of_day":"16:00","end_time_of_day":"18:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: block 9am-5pm Thursday for the offsite
JSON: {"action":"create","confidence":0.9,"draft":{"title":"Offsite","date_expression":{"kin\
d":"weekday","weekday":"thursday"},"time_of_day":"09:00","end_time_of_day":"17:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: gym tomorrow at 7am for an hour
JSON: {"action":"create","confidence":0.9,"draft":{"title":"Gym","date_expression":{"kind":"\
tomorrow"},"time_of_day":"07:00","duration_minutes":60}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: standup every weekday 9:15 for 15 minutes
JSON: {"action":"create","confidence":0.88,"draft":{"title":"Standup","date_expression":{"ki\
nd":"tomorrow"},"time_of_day":"09:15","duration_minutes":15,"recurrence":{"freq":"weekly","b\
y_weekday":["monday","tuesday","wednesday","thursday","friday"]}}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: put something on my calendar next week
JSON: {"action":"create","confidence":0.7,"clarifying_question":"What should I add, and \
which day and time next week?"}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: remind me why we picked SQLite for this project
JSON: {"is_calendar_action":false,"confidence":0.95}

CONTEXT: Now is 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: what's on my calendar this Friday
JSON: {"action":"read","confidence":0.93}

CONTEXT: Now is 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: what events do I have tomorrow
JSON: {"action":"read","confidence":0.93}

CONTEXT: Now is 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: what is a good calendar app?
JSON: {"is_calendar_action":false,"confidence":0.97}

CONTEXT: Now is 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: my calendar is packed this week
JSON: {"is_calendar_action":false,"confidence":0.8}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). Candidate events:
  [{"id": "evt-1", "title": "Dentist appointment", "start_at": "2026-08-28T15:00:00-07:00"}]
USER: actually move my dentist appointment to 4pm
JSON: {"action":"update","confidence":0.88,"event_id":"evt-1","draft":{"title":"Dentist appo\
intment","date_expression":{"kind":"explicit_date","explicit_date":"2026-08-28"},"time_of_da\
y":"16:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). Candidate events: []
USER: cancel my meeting
JSON: {"action":"delete","confidence":0.4,"clarifying_question":"Which meeting would you lik\
e me to cancel?"}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: set up a standing 1:1 every Monday at 9am until the end of the year
JSON: {"action":"create","confidence":0.85,"draft":{"title":"1:1","date_expression":{"kind":\
"weekday","weekday":"monday"},"time_of_day":"09:00","recurrence":{"freq":"weekly","by_weekda\
y":["monday"],"until_expression":{"kind":"end_of_year"}}}}
"""

_DECLARATIVE_REFINEMENT_SYSTEM_PROMPT = """You are Neo's calendar-request classifier, \
taking a second look.

This message was NOT initially recognized as a calendar action. Re-examine it
specifically for one thing: is it a DECLARATIVE STATEMENT describing a dated
personal event -- something a personal assistant should offer to track on the
calendar, even though it is not phrased as a command? For example "I have a
meeting Friday at 3" describes a commitment the same way "remind me to call
the dentist Friday at 3pm" does, just without the imperative verb.

Use the exact same JSON schema and rules as a normal classification:
{
  "is_calendar_action": true|false,
  "action": "read"|"create"|"update"|"delete"|null,
  "confidence": 0.0-1.0,
  "event_id": "<id from the candidate list below>"|null,
  "draft": {
    "title": "<short title>",
    "date_expression": {
      "kind": "today"|"tomorrow"|"day_after_tomorrow"|"yesterday"|"weekday"|"explicit_date",
      "weekday": "monday".."sunday"|null,
      "relative": "this"|"next"|null,
      "explicit_date": "YYYY-MM-DD"|null
    },
    "time_of_day": "HH:MM"|null,
    "end_time_of_day": "HH:MM"|null,
    "duration_minutes": <int>|null,
    "all_day": true|false,
    "location": "<or empty string>",
    "recurrence": {"freq": "daily"|"weekly"|"monthly", "interval": 1,
                    "by_weekday": ["monday".."sunday"]|null,
                    "until_expression": {"kind": "end_of_year"|"end_of_month"|
                      "explicit_date", "explicit_date": "YYYY-MM-DD"}|null,
                    "count": <int>|null} | null
  } | null,
  "clarifying_question": "<a short question>"|null
}

IMPORTANT -- you do NOT calculate dates. Report the date the way the user
expressed it and the application computes the actual calendar date: a named
weekday becomes {"kind": "weekday", "weekday": "<that day>", "relative":
"next" only if they explicitly said "next"}. Never work out which date a
weekday falls on. "time_of_day"/"end_time_of_day" are 24-hour "HH:MM" clock
times ("3pm" -> "15:00"); "time_of_day" is null only when "all_day" is true.
A stated end ("4pm to 6pm") sets "end_time_of_day"; a stated length ("for an
hour") sets "duration_minutes" instead. Never both, and never compute one
from the other.

Rules:
- If this really is a declarative statement about a dated personal event,
  treat it as "create" with a "draft" describing the date the user gave,
  exactly like an imperative request would be.
- If, on this second look, it is still not about the user's own calendar (a
  general remark, a hypothetical, an event with no real date), set
  "is_calendar_action" to false and every other field to null. Do not force a
  "create" just because this is a refinement pass -- staying false is a
  valid, expected outcome most of the time this prompt is even reached.
- Never invent a time. If no date/time is stated or clearly implied, set
  "is_calendar_action" to false rather than guessing.

Examples:
CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: I have a meeting Friday at 3.
JSON: {"action":"create","confidence":0.85,"draft":{"title":"Meeting","date_expression":{"ki\
nd":"weekday","weekday":"friday"},"time_of_day":"15:00"}}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: My calendar is packed this week.
JSON: {"is_calendar_action":false,"confidence":0.85}

CONTEXT: Now is Tuesday 2026-08-25T09:00:00-07:00 (America/Los_Angeles). No candidates.
USER: I've got a dentist appointment tomorrow at 9am.
JSON: {"action":"create","confidence":0.87,"draft":{"title":"Dentist appointment","date_expr\
ession":{"kind":"tomorrow"},"time_of_day":"09:00"}}
"""

_JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)

_DECLARATIVE_EVENT_SHAPE = re.compile(
    r"\bi(?:'ve| have| ?got)\b.{0,60}\b"
    r"(?:meeting|appointment|call|interview|dinner|lunch|deadline|flight|exam|class)\b",
    re.IGNORECASE,
)
_TEMPORAL_EXPRESSION = re.compile(
    r"\b(?:today|tomorrow|mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?|next week|in \d+ (?:days?|weeks?)|"
    r"at \d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b",
    re.IGNORECASE,
)


#: Whole messages that cannot be a calendar request whatever the model says.
#: Matched against the entire normalized message, never against a substring:
#: "hi" skips the classifier, "hi, book me a dentist Friday" does not.
#:
#: Confirmations ("ok", "sure", "yes") are deliberately absent even though
#: they look equally harmless -- they carry meaning against a proposal, and
#: the cost of being wrong there is a change the user did not approve.
_SOCIAL_PLEASANTRIES = frozenset(
    {
        "hi", "hii", "hiya", "hello", "hey", "yo", "howdy", "sup",
        "hi there", "hello there", "hey there",
        "good morning", "good afternoon", "good evening", "good night",
        "thanks", "thank you", "thanks a lot", "thank you so much", "ty", "cheers",
        "bye", "goodbye", "see you", "see ya", "later",
        "how are you", "how are you doing", "hows it going", "how's it going",
        "whats up", "what's up",
        "nice", "cool", "great", "awesome", "perfect", "lovely",
        "no worries", "youre welcome", "you're welcome", "np", "yw",
        "sorry", "my bad", "please",
    }
)


def is_social_pleasantry(prompt: str) -> bool:
    """A greeting or courtesy with nothing else in it.

    This is a path *away* from calendar behavior, never into it, which is why
    a fixed list is safe here when it would not be for recognising a request:
    the worst a false positive can do is answer a greeting without first
    asking a model whether "hello" was an appointment. A false negative costs
    only the round-trip that happens today.

    That round-trip is not free -- it is a separate request that evaluates a
    ~2,600-token system prompt before it can answer "no", several seconds on
    a local model, paid on the message most likely to be someone's first.
    """
    cleaned = " ".join((prompt or "").lower().split()).strip(" .!?,;:-")
    return bool(cleaned) and cleaned in _SOCIAL_PLEASANTRIES


def looks_like_a_declarative_calendar_statement(prompt: str) -> bool:
    """A narrow, deterministic trigger for the declarative-refinement pass.

    Intentionally not a general calendar-language parser: both an event-noun
    shape *and* an explicit temporal expression must be present, so this
    stays a cheap gate for "should we spend one more LLM call double-
    checking this" rather than a classifier in its own right. It decides
    only *when* to ask again -- never *what* the answer is.
    """
    return bool(_DECLARATIVE_EVENT_SHAPE.search(prompt) and _TEMPORAL_EXPRESSION.search(prompt))


_MODIFICATION_SYSTEM_PROMPT = """You are Neo's calendar-proposal modification classifier.

Neo has already shown the user a calendar proposal, and the user has replied
asking for a change to it. That proposal is given below as the BASELINE. Your
only job is to report which fields the user explicitly asked to change. You are
NOT choosing an event -- the baseline is already the event being modified, and
you must never look for or refer to a different one.

Return exactly one compact JSON object, no Markdown, no indentation, and omit
every key the user did not explicitly change:
{"is_modification":true|false,"confidence":0.0-1.0,"changes":{...}}

"changes" may contain only these keys, each only when the user explicitly asked
for that field to change:
  "title": "<the new title the user asked for>"
  "date_expression": {"kind":"today"|"tomorrow"|"day_after_tomorrow"|"yesterday"\
|"weekday"|"explicit_date","weekday":"monday".."sunday","relative":"this"|"next"\
,"explicit_date":"YYYY-MM-DD"}
  "time_of_day": "HH:MM"       (24-hour clock)
  "end_time_of_day": "HH:MM"   (a stated end: "until 6pm", "to 18:00")
  "duration_minutes": <int>    (a stated length: "for an hour" -> 60)
  "location": "<the new location>"
  "all_day": true|false

Rules:
- You do NOT calculate dates. Report a changed date the way the user expressed
  it; the application resolves it deterministically.
- Omit "date_expression" when the user did not change the date -- the
  baseline's date is preserved automatically. Same for every other key.
- Omit "title" unless the user explicitly asked to rename the event. Never echo
  the baseline title back, and never substitute another event's title.
- Give an end as "end_time_of_day" OR a length as "duration_minutes", never
  both, and never calculate one from the other. Omit both when the user only
  moved the start: an event that already has an end keeps its length
  automatically, so naming one here would change something they did not ask
  to change.
- If the reply requests no concrete change, set "is_modification" to false.
- Set "confidence" below 0.6 when you are unsure.

Examples:
BASELINE: create "Dentist appointment" starting 2026-08-27T15:00:00+00:00
USER: yes, but make it 4pm
JSON: {"is_modification":true,"confidence":0.95,"changes":{"time_of_day":"16:00"}}

BASELINE: create "Dentist appointment" starting 2026-08-27T15:00:00+00:00
USER: move it to Friday at 5pm
JSON: {"is_modification":true,"confidence":0.93,"changes":{"date_expression":\
{"kind":"weekday","weekday":"friday"},"time_of_day":"17:00"}}

BASELINE: create "Dentist appointment" starting 2026-08-27T15:00:00+00:00
USER: change the title to Eye doctor
JSON: {"is_modification":true,"confidence":0.94,"changes":{"title":"Eye doctor"}}

BASELINE: create "Work on neo" starting 2026-09-01T16:00:00+05:30
USER: make it end at 7pm instead
JSON: {"is_modification":true,"confidence":0.93,"changes":{"end_time_of_day":"19:00"}}

BASELINE: create "Work on neo" starting 2026-09-01T16:00:00+05:30
USER: actually just an hour
JSON: {"is_modification":true,"confidence":0.9,"changes":{"duration_minutes":60}}

BASELINE: create "Work on neo" starting 2026-09-01T16:00:00+05:30
USER: push it to 5pm
JSON: {"is_modification":true,"confidence":0.93,"changes":{"time_of_day":"17:00"}}

BASELINE: create "Dentist appointment" starting 2026-08-27T15:00:00+00:00
USER: make it Friday at 4pm and call it Eye doctor
JSON: {"is_modification":true,"confidence":0.92,"changes":{"title":"Eye doctor",\
"date_expression":{"kind":"weekday","weekday":"friday"},"time_of_day":"16:00"}}

BASELINE: update "Team sync" starting 2026-09-01T09:00:00+00:00
USER: actually never mind what is a mutex
JSON: {"is_modification":false,"confidence":0.9}
"""


class ProposalModificationDecision(BaseModel):
    """Only the fields the user explicitly asked to change. Deliberately not a
    full draft: identity (action, event_id, and every untouched field) comes
    from the stored proposal, never from the model."""

    is_modification: bool = False
    confidence: float = 0.0
    changes: dict = Field(default_factory=dict)


class CalendarDraftDecision(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _imply_action_flag(cls, data: object) -> object:
        """Treat a returned ``action`` as implying ``is_calendar_action``.

        Every token the model does not have to emit is a token it cannot
        truncate or mis-quote, and this flag is fully redundant with
        ``action`` in the affirmative case. Saying ``false`` is still
        explicit and still wins -- only the omitted case is inferred, so the
        external contract is unchanged and nothing is guessed.
        """
        if isinstance(data, dict) and "is_calendar_action" not in data:
            return {**data, "is_calendar_action": data.get("action") is not None}
        return data

    is_calendar_action: bool = False
    action: CalendarAction | None = None
    confidence: float = 0.0
    event_id: str | None = None
    draft: dict | None = None
    clarifying_question: str | None = None


class CalendarIntentClassifier:
    """Asks the selected chat model whether a message wants a calendar change."""

    MIN_CONFIDENCE = 0.55

    def __init__(self) -> None:
        #: Set when the most recent classification declined *because* a draft
        #: the model had already committed to carried a date or time that
        #: could not be resolved exactly. ``classify()`` still returns ``None``
        #: -- its fail-closed contract is unchanged -- but "declined because
        #: this was not a calendar message" and "declined rather than invent a
        #: date for a calendar message" are different facts, and only the
        #: second one means a calendar change was requested and not made.
        self.last_unresolved_mutation = False

    def classify(
        self,
        prompt: str,
        *,
        llm: LLMClient | None,
        now: datetime,
        timezone_label: str,
        candidate_events: list[dict],
    ) -> CalendarDraftDecision | None:
        return self._classify_with_prompt(
            _SYSTEM_PROMPT,
            prompt,
            llm=llm,
            now=now,
            timezone_label=timezone_label,
            candidate_events=candidate_events,
        )

    def classify_declarative(
        self,
        prompt: str,
        *,
        llm: LLMClient | None,
        now: datetime,
        timezone_label: str,
        candidate_events: list[dict],
    ) -> CalendarDraftDecision | None:
        """One bounded refinement pass for a declarative statement `classify()`
        already declined. Same fail-closed contract, same JSON schema, a
        different system prompt -- never a mutation of `classify()`'s own
        prompt or behavior. Callers decide *when* this runs; it never calls
        itself or `classify()` again."""
        return self._classify_with_prompt(
            _DECLARATIVE_REFINEMENT_SYSTEM_PROMPT,
            prompt,
            llm=llm,
            now=now,
            timezone_label=timezone_label,
            candidate_events=candidate_events,
        )

    def classify_modification(
        self,
        prompt: str,
        *,
        llm: LLMClient | None,
        now: datetime,
        baseline_action: str,
        baseline_title: str | None,
        baseline_draft: dict,
    ) -> ProposalModificationDecision | None:
        """Ask which fields of an already-shown proposal the user changed.

        The pending proposal is supplied as the baseline, so the model never
        searches the calendar and never picks which event is being modified --
        that identity is already settled. Returns ``None`` (fail closed) on a
        parse error, low confidence, a non-modification, or an empty change
        set, in which case the caller keeps today's behavior.
        """
        if llm is None or not prompt.strip():
            return None
        title = baseline_title or baseline_draft.get("title") or "that event"
        start_at = baseline_draft.get("start_at")
        baseline = f'{baseline_action} "{title}"' + (
            f" starting {start_at}" if start_at else ""
        )
        try:
            raw = llm.chat(
                [
                    LLMMessage(role="system", content=_MODIFICATION_SYSTEM_PROMPT),
                    LLMMessage(
                        role="user",
                        content=f"BASELINE: {baseline}\nUSER: {prompt}",
                    ),
                ],
                temperature=0.0,
            )
            cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
            match = _JSON_BLOB.search(cleaned)
            payload = json.loads(match.group(0) if match else cleaned.strip())
            decision = ProposalModificationDecision.model_validate(payload)
        except (AttributeError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return None
        if not decision.is_modification or decision.confidence < self.MIN_CONFIDENCE:
            return None
        if not decision.changes:
            return None
        return decision

    def _classify_with_prompt(
        self,
        system_prompt: str,
        prompt: str,
        *,
        llm: LLMClient | None,
        now: datetime,
        timezone_label: str,
        candidate_events: list[dict],
    ) -> CalendarDraftDecision | None:
        self.last_unresolved_mutation = False
        if llm is None or not prompt.strip():
            return None
        context = (
            f"Now is {now.strftime('%A')} {now.isoformat()} ({timezone_label}). "
            f"Candidate events: {json.dumps(candidate_events)}"
        )
        try:
            raw = llm.chat(
                [
                    LLMMessage(role="system", content=system_prompt),
                    LLMMessage(role="user", content=f"CONTEXT: {context}\nUSER: {prompt}"),
                ],
                temperature=0.0,
            )
            cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
            match = _JSON_BLOB.search(cleaned)
            payload = json.loads(match.group(0) if match else cleaned.strip())
            decision = CalendarDraftDecision.model_validate(payload)
        except (AttributeError, TypeError, ValueError, ValidationError, json.JSONDecodeError):
            return None
        if not decision.is_calendar_action:
            return None
        if decision.confidence < self.MIN_CONFIDENCE and decision.clarifying_question is None:
            return None
        if decision.draft:
            resolved = _resolve_draft_dates(decision.draft, now=now)
            if resolved is None:
                # A date/time was intended but could not be resolved exactly.
                # Fail the whole decision closed rather than scheduling
                # something on a guessed day.
                self.last_unresolved_mutation = decision.action in CALENDAR_MUTATING_ACTIONS
                return None
            decision = decision.model_copy(update={"draft": resolved})
        return decision
