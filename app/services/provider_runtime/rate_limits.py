from __future__ import annotations

from datetime import UTC, datetime

DEFAULT_RPM = 60
DEFAULT_TPM = 100_000
DEFAULT_DAILY = 10_000


def window_key(seconds: int) -> str:
    now = int(datetime.now(UTC).timestamp())
    return datetime.fromtimestamp(now - now % seconds, UTC).isoformat()


def decision(records: list[dict], route_name: str, tokens: int) -> dict:
    minute = next((row for row in records if row["window_seconds"] == 60), None)
    day = next((row for row in records if row["window_seconds"] == 86400), None)
    minute_requests, minute_tokens = (
        (minute or {}).get("request_count", 0),
        (minute or {}).get("token_count", 0),
    )
    daily_requests = (day or {}).get("request_count", 0)
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
