from __future__ import annotations

import re
from typing import Any

_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|token|password|authorization|cookie)\s*[=:]\s*"
    r"(?:bearer\s+)?[^\s,;]+"
)
_ABS_PATH = re.compile(r"(?:/Users/[^\s]+|/home/[^\s]+|[A-Za-z]:\\[^\s]+)")
_SENSITIVE_KEYS = {"api_key", "secret", "token", "authorization", "cookie", "password", "headers"}


def safe_text(value: Any, limit: int = 4_000) -> tuple[str, dict[str, int | bool]]:
    raw = str(value or "")
    secrets = len(_SECRET.findall(raw))
    paths = len(_ABS_PATH.findall(raw))
    text = _SECRET.sub("[REDACTED]", raw)
    text = _ABS_PATH.sub("[workspace path]", text)
    return re.sub(r"\s+", " ", text).strip()[:limit], {
        "redacted": bool(secrets or paths),
        "secret_redactions": secrets,
        "path_redactions": paths,
    }


def safe_value(value: Any) -> tuple[Any, dict[str, int | bool]]:
    if isinstance(value, dict):
        output, count = {}, {"redacted": False, "secret_redactions": 0, "path_redactions": 0}
        for key, item in value.items():
            if str(key).lower() in _SENSITIVE_KEYS:
                output[str(key)] = "[REDACTED]"
                count["redacted"] = True
                count["secret_redactions"] += 1
            else:
                output[str(key)], child = safe_value(item)
                count["redacted"] = bool(count["redacted"] or child["redacted"])
                count["secret_redactions"] += int(child["secret_redactions"])
                count["path_redactions"] += int(child["path_redactions"])
        return output, count
    if isinstance(value, list):
        result, count = [], {"redacted": False, "secret_redactions": 0, "path_redactions": 0}
        for item in value:
            safe, child = safe_value(item)
            result.append(safe)
            count["redacted"] = bool(count["redacted"] or child["redacted"])
            count["secret_redactions"] += int(child["secret_redactions"])
            count["path_redactions"] += int(child["path_redactions"])
        return result, count
    return (
        safe_text(value)
        if isinstance(value, str)
        else (value, {"redacted": False, "secret_redactions": 0, "path_redactions": 0})
    )
