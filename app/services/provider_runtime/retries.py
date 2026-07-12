from __future__ import annotations

from app.services.provider_runtime.errors import classify

MAX_RETRIES = 2
BASE_BACKOFF_MS = 500
MAX_BACKOFF_MS = 4000


def retryable(exc: Exception) -> bool:
    return classify(exc) in {"timeout", "rate_limited", "transient_network"}


def backoff_ms(attempt: int) -> int:
    return min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * (2**attempt))
