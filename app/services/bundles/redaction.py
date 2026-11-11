from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

REDACTED = "[REDACTED]"
SENSITIVE = re.compile(
    r"api[_-]?key|secret|token|authorization|cookie|password|credential|env", re.I
)
#: Secrets by *shape*, wherever they appear: a task description, a note body, a
#: tool's output, a chat message. Redacting by field name alone exported an API
#: key verbatim the moment it was pasted into free text.
SECRET_VALUE = re.compile(
    "|".join(
        (
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
            r"\bsk-or-v1-[A-Za-z0-9]{20,}\b",  # OpenRouter
            r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b",  # OpenAI, Anthropic
            r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b",  # GitHub
            r"\bgithub_pat_[A-Za-z0-9_]{40,}\b",
            r"\btvly-[A-Za-z0-9_-]{20,}\b",  # Tavily
            r"\bAIza[0-9A-Za-z_-]{30,}\b",  # Google API key
            r"\bya29\.[0-9A-Za-z_-]{30,}\b",  # Google OAuth access token
            r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",  # AWS access key id
            r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b",  # Slack
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b",  # JWT
            r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}",
            # key=value pairs naming a credential, with the value redacted whole.
            r"(?i:\b(?:api[_-]?key|secret|token|password|passwd|pwd|authorization)"
            r"\s*[=:]\s*[\"']?[^\s\"',;]{6,})",
        )
    )
)
_counter: dict[str, int] = {"values": 0}


def redact_text(value: str) -> str:
    """Remove token-shaped values and absolute paths from free text."""

    def replace(match: re.Match) -> str:
        _counter["values"] += 1
        text = match.group(0)
        separator = re.match(r"(?i)^[a-z_-]+\s*[=:]\s*", text)
        if separator and not text.startswith("Bearer"):
            return separator.group(0) + REDACTED
        return REDACTED

    return ABSOLUTE_PATH.sub(REDACTED, SECRET_VALUE.sub(replace, value))


def redacted_value_count() -> int:
    """How many token-shaped values have been redacted so far in this process."""

    return _counter["values"]
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
        return redact_text(value)
    return value


def safe_archive_name(name: str) -> str:
    path = PurePath(name)
    if path.is_absolute() or ".." in path.parts or name.startswith(("/", "\\")):
        raise ValueError(f"Unsafe archive path: {name}")
    return name
