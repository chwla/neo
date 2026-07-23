"""Conservative routing for chat requests that target Neo's internal panels.

Internal data panels are useful shortcuts, but they must never replace a normal
assistant answer just because a prompt discusses the same subject. This module
therefore recognises only clear, command-shaped requests for a panel lookup or
operation. Explanations, documentation requests, comparisons, and other
topic-level prompts always stay on the normal LLM route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

InternalFeature = Literal["recovery", "coding", "git", "tests", "tasks"]
InternalAction = Literal["lookup", "operation"]


@dataclass(frozen=True)
class InternalChatIntent:
    """A high-confidence request for a read-only panel response or operation."""

    feature: InternalFeature
    action: InternalAction


_COMMAND_PREFIX = re.compile(
    r"^(?:please\s+)?(?P<verb>find|show|list|check|open|view|inspect|get|"
    r"create|run|resume|retry|fork|repair)\b",
    re.IGNORECASE,
)
_EXPLANATORY_REQUEST = re.compile(
    r"\b(?:explain|describe|compare|documentation|document|purpose|"
    r"how\s+(?:should|does|do|would)|what\s+is|why|write\s+(?:a\s+)?(?:doc|guide))\b",
    re.IGNORECASE,
)

_FEATURE_TARGETS: tuple[tuple[InternalFeature, re.Pattern[str]], ...] = (
    (
        "recovery",
        re.compile(
            r"\b(?:recovery(?:\s+page)?|recoverable|resumable|interrupted|"
            r"incomplete)\b(?:\s+(?:coding[-\s]?agent|agent))?\s+runs?\b|"
            r"\brecovery(?:\s+page)?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "coding",
        re.compile(
            r"\b(?:coding[-\s]?agent|coding\s+runs?|patch\s+actions?|"
            r"coding\s+checkpoint)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "git",
        re.compile(
            r"\b(?:git\s+status|repo(?:sitory)?\s+diff|git\s+diff|"
            r"git\s+checkpoints?|rollback|roll\s+back)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tests",
        re.compile(
            r"\b(?:test\s+runs?|test\s+history|failed\s+tests?|"
            r"test\s+results?|run\s+(?:the\s+)?saved\s+test\s+command)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "tasks",
        re.compile(
            r"\b(?:my|stored|project)?\s*(?:tasks?|to-?dos?)\b|"
            r"\b(?:blocked|open|pending|active|critical|completed)\s+tasks?\b",
            re.IGNORECASE,
        ),
    ),
)


def resolve_internal_chat_intent(prompt: str) -> InternalChatIntent | None:
    """Return an internal-panel intent only for an unambiguous user command.

    A feature name alone is deliberately insufficient. For example, "Explain
    recovery after restart" is about recovery, whereas "Find my recoverable
    runs" asks Neo to inspect stored recovery data.
    """

    text = re.sub(r"\s+", " ", (prompt or "").strip())
    if not text or _EXPLANATORY_REQUEST.search(text):
        return None
    command = _COMMAND_PREFIX.match(text)
    if command is None:
        return None

    action: InternalAction = (
        "operation"
        if command.group("verb").lower()
        in {"create", "run", "resume", "retry", "fork", "repair"}
        else "lookup"
    )
    for feature, target in _FEATURE_TARGETS:
        if target.search(text):
            return InternalChatIntent(feature=feature, action=action)
    return None


def is_internal_chat_command(prompt: str, feature: InternalFeature) -> bool:
    """Whether *prompt* explicitly requests the given internal feature."""

    intent = resolve_internal_chat_intent(prompt)
    return intent is not None and intent.feature == feature
