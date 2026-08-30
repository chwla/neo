from __future__ import annotations

from datetime import UTC, datetime

DEFAULT_RPM = 60
DEFAULT_TPM = 100_000
DEFAULT_DAILY = 10_000


def window_key(seconds: int, now: datetime | None = None) -> str:
    """The start of the fixed window ``now`` falls in, as an ISO timestamp.

    ``now`` exists so callers -- tests especially -- can evaluate a window
    without waiting for the clock. Production callers pass nothing and get the
    current window.
    """
    moment = int((now or datetime.now(UTC)).timestamp())
    return datetime.fromtimestamp(moment - moment % seconds, UTC).isoformat()


def _totals(records: list[dict], seconds: int, start: str) -> tuple[int, int]:
    """Requests and tokens recorded in exactly one window.

    Summed rather than picked: ``record_rate`` upserts with a SELECT followed
    by an INSERT and no unique constraint, so two concurrent writers can each
    miss and each insert a row for the same window. Adding them together keeps
    the count right when that happens, and makes the result independent of the
    order the rows come back in.
    """
    requests = tokens = 0
    for row in records:
        if row.get("window_seconds") == seconds and row.get("window_start") == start:
            requests += row.get("request_count") or 0
            tokens += row.get("token_count") or 0
    return requests, tokens


def decision(
    records: list[dict], route_name: str, tokens: int, now: datetime | None = None
) -> dict:
    """Whether this request fits the route's budget for the window it lands in.

    ``records`` is every rate row stored for the route, across every window
    ever recorded -- nothing prunes them. So the window each limit applies to
    has to be selected here by its start key. Matching on
    ``window_seconds`` alone took whichever row the database happened to
    return first, which meant an expired window that had once hit the cap
    could block an empty current window indefinitely, and a real burst in the
    current window could go unenforced. Selecting by window start makes an
    expired window simply stop matching, so a new window naturally starts from
    zero.
    """
    minute_start = window_key(60, now)
    day_start = window_key(86400, now)
    minute_requests, minute_tokens = _totals(records, 60, minute_start)
    daily_requests, _ = _totals(records, 86400, day_start)
    blocked = (
        minute_requests >= DEFAULT_RPM
        or minute_tokens + tokens > DEFAULT_TPM
        or daily_requests >= DEFAULT_DAILY
    )
    return {
        "allowed": not blocked,
        "reason": "soft route limit exceeded" if blocked else None,
        "reset_estimate_seconds": 60 if blocked else 0,
        "route_name": route_name,
    }
