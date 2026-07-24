from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize_memory_value(value: object) -> str:
    """Normalize user-authored memory values for equality, never for display."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.casefold().replace("’", "'")
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n.,;:!?")
    return text


def memory_fingerprint(namespace: str, *values: object) -> str:
    """Create a stable content identity used by typed records and durable memories."""

    parts = [normalize_memory_value(namespace)]
    parts.extend(normalize_memory_value(value) for value in values)
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_fingerprint(
    source_message_id: int | None,
    source_conversation_id: int | None,
    source_sentence: str,
) -> str:
    """Identify a supporting user message while retaining a safe legacy fallback."""

    if source_message_id is not None:
        return memory_fingerprint("message", source_message_id)
    return memory_fingerprint("conversation-source", source_conversation_id, source_sentence)
