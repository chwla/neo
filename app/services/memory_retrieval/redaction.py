from __future__ import annotations

import json
from typing import Any

from app.services.context_memory.redaction import redact


def redact_memory(value: Any) -> tuple[Any, dict[str, int]]:
    """Redact before persistence and expose only aggregate redaction metadata."""
    before = json.dumps(value, default=str, sort_keys=True)
    safe = redact(value)
    after = json.dumps(safe, default=str, sort_keys=True)
    return safe, {
        "credential_or_env_redactions": max(0, after.count("[REDACTED")),
        "redacted": before != after,
    }
