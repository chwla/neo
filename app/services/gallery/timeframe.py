"""Turning "last week" in a query into a window over when an image was seen.

Deliberately deterministic and model-free. The calendar's date handling resolves
a structured expression that an LLM has already produced, which is the right
shape there and the wrong one here: search must not need a model round trip to
narrow a result set, and must behave identically on every run.

Only phrases with one unambiguous reading are matched. Anything else returns
None, and the query is searched unfiltered -- a wrong window silently hides the
photo the user is looking for, which is worse than no window at all.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

_MONTHS = {name.lower(): index for index, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): index for index, name in enumerate(calendar.month_abbr) if name})

_WEEKDAYS = {name.lower(): index for index, name in enumerate(calendar.day_name)}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    #: The phrase that produced the window, so the caller can strip it from the
    #: text it sends to the ranker: "last week" contributes nothing lexically and
    #: would only dilute the terms that matter.
    phrase: str

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()


def _span(start_day: date, end_day: date, phrase: str) -> Window:
    """A closed span of whole days, in UTC, matching how seen_at is stored."""

    return Window(
        start=datetime.combine(start_day, time.min, tzinfo=UTC),
        end=datetime.combine(end_day, time.max, tzinfo=UTC),
        phrase=phrase,
    )


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_span(year: int, month: int, phrase: str) -> Window:
    last = calendar.monthrange(year, month)[1]
    return _span(date(year, month, 1), date(year, month, last), phrase)


def parse_window(query: str, now: datetime | None = None) -> Window | None:
    """The time range a query asks for, or None when it does not ask for one."""

    if not query or not query.strip():
        return None
    now = now or datetime.now(UTC)
    today = now.date()
    text = query.lower()

    if match := re.search(r"\b(today)\b", text):
        return _span(today, today, match.group(1))
    if match := re.search(r"\b(yesterday)\b", text):
        return _span(today - timedelta(days=1), today - timedelta(days=1), match.group(1))

    # "last week" is the previous calendar week, not the trailing seven days.
    # People saying it mean the week that ended, and including today would let a
    # photo from this morning answer a question about last week.
    if match := re.search(r"\b(last week)\b", text):
        this_week = _week_start(today)
        return _span(this_week - timedelta(days=7), this_week - timedelta(days=1), match.group(1))
    if match := re.search(r"\b(this week)\b", text):
        return _span(_week_start(today), today, match.group(1))
    if match := re.search(r"\b(last month)\b", text):
        first = today.replace(day=1)
        previous = first - timedelta(days=1)
        return _month_span(previous.year, previous.month, match.group(1))
    if match := re.search(r"\b(this month)\b", text):
        return _span(today.replace(day=1), today, match.group(1))
    if match := re.search(r"\b(last year)\b", text):
        year = today.year - 1
        return _span(date(year, 1, 1), date(year, 12, 31), match.group(1))

    if match := re.search(r"\b(?:in the )?(?:last|past) (\d{1,3}) (day|week|month)s?\b", text):
        count = int(match.group(1))
        unit = match.group(2)
        days = count * {"day": 1, "week": 7, "month": 30}[unit]
        return _span(today - timedelta(days=days), today, match.group(0))

    if match := re.search(r"\b(?:on|last) (" + "|".join(_WEEKDAYS) + r")\b", text):
        target = _WEEKDAYS[match.group(1)]
        delta = (today.weekday() - target) % 7 or 7
        day = today - timedelta(days=delta)
        return _span(day, day, match.group(0))

    # "in March", "on 3 August", "on August 3"
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    if match := re.search(rf"\b(\d{{1,2}}) ({month_names})\b", text):
        day_number, month = int(match.group(1)), _MONTHS[match.group(2)]
        year = today.year if month <= today.month else today.year - 1
        try:
            day = date(year, month, day_number)
        except ValueError:
            return None
        return _span(day, day, match.group(0))
    if match := re.search(rf"\b({month_names}) (\d{{1,2}})\b", text):
        month, day_number = _MONTHS[match.group(1)], int(match.group(2))
        year = today.year if month <= today.month else today.year - 1
        try:
            day = date(year, month, day_number)
        except ValueError:
            return None
        return _span(day, day, match.group(0))
    if match := re.search(rf"\bin ({month_names})\b", text):
        month = _MONTHS[match.group(1)]
        year = today.year if month <= today.month else today.year - 1
        return _month_span(year, month, match.group(0))

    return None


def strip_phrase(query: str, window: Window | None) -> str:
    """The query with its time phrase removed, for lexical matching."""

    if not window:
        return query
    cleaned = re.sub(re.escape(window.phrase), " ", query, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", cleaned).strip() or query
