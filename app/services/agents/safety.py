"""Strict allowlist for Agent Runner v1. No external action tools exist here."""

from __future__ import annotations

ALLOWED_STEP_TYPES = {
    "plan",
    "read_context",
    "think",
    "web_search",
    "research",
    "draft",
    "summarize",
    "save_note",
    "task_update_request",
    "patch_proposal",
    "final",
}
APPROVAL_REQUIRED_STEP_TYPES = {"save_note", "task_update_request"}
FORBIDDEN_ACTIONS = {
    "shell",
    "terminal",
    "file_write",
    "file_delete",
    "browser",
    "email",
    "purchase",
    "memory_write",
    "archive",
    "delete",
}


class AgentSafetyError(ValueError):
    pass


def validate_plan(plan: list[dict]) -> list[dict]:
    if not 3 <= len(plan) <= 7:
        raise AgentSafetyError("Agent plans must contain between 3 and 7 steps.")
    cleaned: list[dict] = []
    for raw in plan:
        step_type = str(raw.get("type", "")).strip()
        if step_type not in ALLOWED_STEP_TYPES:
            raise AgentSafetyError(f"Step type '{step_type}' is not allowed.")
        if step_type in FORBIDDEN_ACTIONS:
            raise AgentSafetyError(f"Action '{step_type}' is forbidden.")
        requires_approval = bool(raw.get("requires_approval", False))
        if step_type in APPROVAL_REQUIRED_STEP_TYPES:
            requires_approval = True
        cleaned.append(
            {
                "title": str(raw.get("title") or step_type.replace("_", " ").title())[:200],
                "type": step_type,
                "requires_approval": requires_approval,
            }
        )
    return cleaned


def runner_system_prompt() -> str:
    return (
        "You are Neo Agent Runner v1. You work only on the selected task. "
        "Use only the provided task, project, note, and run context. Never claim to have "
        "edited files, sent messages, changed tasks, or performed external actions. "
        "Do not invent sources. Do not write to Memory. Do not perform destructive actions. "
        "Do not propose shell, terminal, browser, email, purchase, or filesystem actions as completed work. "
        "Produce a concise, useful output and state what information is missing."
    )
