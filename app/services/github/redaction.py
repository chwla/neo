from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(r"token|authorization|cookie|secret|password", re.I)


def redact(value: Any, key: str = "") -> Any:
    if key == "token_ref":
        return value
    if SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value
