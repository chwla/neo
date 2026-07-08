from __future__ import annotations

import re

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


def clean_objective(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    if not cleaned:
        raise ValueError("Coding objective is required.")
    if len(cleaned) > 10_000:
        raise ValueError("Coding objective is too long.")
    return cleaned


def require_confirmation(confirm: bool) -> None:
    if confirm is not True:
        raise ValueError("This coding-agent action requires confirm=true.")


def require_pending(action: dict) -> None:
    if action["status"] != "pending":
        raise ValueError("This action request is no longer pending.")
