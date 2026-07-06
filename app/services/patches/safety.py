from __future__ import annotations

import re

MAX_FILES = 10
MAX_TOTAL_CONTEXT_CHARS = 100_000
MAX_SINGLE_FILE_CHARS = 30_000

FORBIDDEN_CLAIMS = (
    (
        re.compile(r"\b(?:i|we) (?:have )?(?:applied|edited|modified|updated|changed)\b", re.I),
        "The following changes are proposed.",
    ),
    (re.compile(r"\btests? (?:have )?passed\b", re.I), "Validation is still needed."),
    (
        re.compile(r"\b(?:i|we) ran (?:the )?(?:tests?|app)\b", re.I),
        "No tests or application execution were performed.",
    ),
)


def clean_objective(value: str) -> str:
    objective = value.strip()
    if not objective:
        raise ValueError("Objective is required.")
    return objective


def remove_execution_claims(content: str) -> str:
    result = content
    for pattern, replacement in FORBIDDEN_CLAIMS:
        result = pattern.sub(replacement, result)
    return result


def has_reliable_unified_diff(content: str, filenames: list[str]) -> bool:
    if not all(marker in content for marker in ("diff --git ", "--- ", "+++ ", "@@")):
        return False
    return any(filename in content for filename in filenames)
