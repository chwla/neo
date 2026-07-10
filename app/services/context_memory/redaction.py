from __future__ import annotations

import re
from typing import Any

SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|authorization|cookie)\b\s*[:=]\s*[^\s,;]+"
)
ENV = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\s*=\s*[^\s,;]+")
ABSOLUTE = re.compile(r"(?<![\w.])(?:/[\w.~ -]+){2,}|[A-Za-z]:\\[^\s,;]+")


def redact_text(value: object) -> str:
    text = str(value or "")
    text = SECRET.sub("[REDACTED_CREDENTIAL]", text)
    text = ENV.sub("[REDACTED_ENV]", text)
    return ABSOLUTE.sub("[REDACTED_PATH]", text)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact(item) for key, item in value.items() if not _secret_key(str(key))}
    return value


def _secret_key(key: str) -> bool:
    return any(
        part in key.lower()
        for part in ("api_key", "secret", "token", "password", "authorization", "cookie")
    )
