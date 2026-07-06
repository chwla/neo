from __future__ import annotations

from app.services.code_index.safety import workspace_text

MAX_REFERENCE_CONTEXT_CHARS = 500


def awareness_text(file_item: dict) -> str:
    return workspace_text(file_item)


def bounded_context(line: str) -> str:
    return line.strip()[:MAX_REFERENCE_CONTEXT_CHARS]
