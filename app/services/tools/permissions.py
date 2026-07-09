from __future__ import annotations

from typing import Any

SAFE_AUTO_CATEGORIES = {"read_only", "workspace_read", "external_read"}
APPROVAL_CATEGORIES = {
    "workspace_write_approval_required",
    "external_write_approval_required",
}


class ToolPermissionError(ValueError):
    pass


def input_is_bounded(payload: dict[str, Any]) -> bool:
    encoded = str(payload)
    return len(encoded) <= 20000


def approval_required(tool: dict, server: dict | None) -> bool:
    if tool["category"] in APPROVAL_CATEGORIES:
        return True
    if server and server.get("approval_required") and tool["category"] not in SAFE_AUTO_CATEGORIES:
        return True
    return False


def ensure_tool_allowed(
    *,
    tool: dict,
    server: dict | None,
    agent: dict | None,
    skill: dict | None,
    input_payload: dict[str, Any],
) -> None:
    if not tool.get("enabled"):
        raise ToolPermissionError("Tool is disabled.")
    if tool["category"] == "dangerous_disabled":
        raise ToolPermissionError("Dangerous tools are disabled.")
    if server and not server.get("enabled"):
        raise ToolPermissionError("Tool server is disabled.")
    if not input_is_bounded(input_payload):
        raise ToolPermissionError("Tool input is too large.")
    if agent is not None:
        allowed = set(agent.get("tools") or [])
        if allowed and tool["id"] not in allowed and tool["name"] not in allowed:
            raise ToolPermissionError("Agent is not allowed to use this tool.")
        if not allowed and tool["category"] not in {"read_only", "workspace_read"}:
            raise ToolPermissionError("Agent must explicitly allow non-read-only tools.")
    if skill is not None:
        allowed_tools = set(skill.get("tool_ids") or [])
        if allowed_tools and tool["id"] not in allowed_tools and tool["name"] not in allowed_tools:
            raise ToolPermissionError("Skill does not allow this tool.")
        if agent is not None:
            allowed_skills = set(agent.get("skills") or [])
            if (
                allowed_skills
                and skill["id"] not in allowed_skills
                and skill["name"] not in allowed_skills
            ):
                raise ToolPermissionError("Agent is not allowed to use this skill.")
