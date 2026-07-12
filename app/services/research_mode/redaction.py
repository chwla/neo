"""Small, centralized redaction boundary for persisted research data."""

from __future__ import annotations

import re
from typing import Any

_SECRET = re.compile(r"(?i)(?:api[_-]?key|token|password|authorization|cookie)\s*[=:]\s*[^\s,;]+")
_ABS_PATH = re.compile(r"(?:/Users/[^\s]+|/home/[^\s]+|[A-Za-z]:\\[^\s]+)")


def safe_text(value: Any, limit: int = 2_000) -> str:
    text = str(value or "")
    text = _SECRET.sub("[REDACTED]", text)
    text = _ABS_PATH.sub("[workspace path]", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): safe_value(item)
            for key, item in value.items()
            if str(key).lower()
            not in {"api_key", "secret", "token", "authorization", "cookie", "password"}
        }
    if isinstance(value, list):
        return [safe_value(item) for item in value]
    return safe_text(value) if isinstance(value, str) else value
