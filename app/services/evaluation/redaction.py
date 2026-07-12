from __future__ import annotations

import re
from typing import Any

SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|password|authorization|cookie|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[^\s,]+"
)
ABS_PATH = re.compile(r"(?:/Users/[^\s,]+|/home/[^\s,]+|[A-Za-z]:\\\\[^\s,]+)")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (
                "[REDACTED]"
                if k.lower()
                in {"api_key", "secret", "password", "authorization", "cookie", "token"}
                else redact(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return ABS_PATH.sub("[REDACTED_PATH]", SECRET.sub("[REDACTED]", value))
    return value


def has_leak(value: Any) -> bool:
    text = str(value)
    return bool(SECRET.search(text) or ABS_PATH.search(text))
