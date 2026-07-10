from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(
    r"api[_-]?key|secret|token|authorization|cookie|password|credential|env", re.I
)
# A path must start at a value boundary, so URL paths (for example https://host/api)
# remain useful metadata while host-local absolute paths are never portable evidence.
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_:/])(?:[A-Za-z]:[\\/]|/)[^\s\"']+")


def redact(value: Any, key: str = "") -> Any:
    """Remove credential values and host-specific absolute paths recursively."""
    if SENSITIVE.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(name): redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, str):
        return ABSOLUTE_PATH.sub(REDACTED, value)
    return value


def safe_archive_name(name: str) -> str:
    path = PurePath(name)
    if path.is_absolute() or ".." in path.parts or name.startswith(("/", "\\")):
        raise ValueError(f"Unsafe archive path: {name}")
    return name
