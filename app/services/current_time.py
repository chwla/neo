"""A trusted calendar date for model prompts, in the user's timezone when known."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def today_for_prompt(timezone: str | None = None) -> str:
    try:
        zone = ZoneInfo(timezone) if timezone else UTC
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        zone = UTC
    return datetime.now(zone).date().isoformat()
