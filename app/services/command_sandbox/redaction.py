from __future__ import annotations

from app.services.context_memory.redaction import redact_text


def redact_output(value: str) -> tuple[str, dict[str, object]]:
    redacted = redact_text(value)
    return redacted, {
        "credentials": "[REDACTED_CREDENTIAL]" in redacted,
        "absolute_paths": "[REDACTED_PATH]" in redacted,
    }
