"""Builds the per-chat tool catalog (the Tools panel's toggle-annotated list).

This used to also bridge Neo's connector-tool system (user-added MCP/REST
tools) into the agent loop's tool set. That system has been removed --
``chat_tools_catalog`` now only ever sees built-in tools.
"""

from __future__ import annotations

from app.services.agent_core.tools.registry import TOOL_TO_GROUP, effective_disabled_tools
from app.services.agent_core.types import PermissionMode


def chat_tools_catalog(
    registry, disabled: set[str], *, mode: PermissionMode, has_repo: bool, live: bool,
    skills: list[dict] | None = None,
) -> list[dict]:
    """Report tool preferences separately from availability in the current workspace."""

    disabled_tools = effective_disabled_tools(disabled)
    entries = []
    for tool in registry.all():
        reason = registry.unavailable_reason(
            tool, mode=mode, has_repo=has_repo, live=live, skills=skills
        )
        entries.append(
            {
                "name": tool.name,
                "display_name": tool.name,
                "description": tool.description,
                "risk": tool.risk,
                "category": tool.category,
                "requires_repo": tool.requires_repo,
                "source": tool.source,
                "enabled": tool.name not in disabled_tools,
                "available": reason is None and tool.name not in disabled_tools,
                "unavailable_reason": reason,
                "group": TOOL_TO_GROUP.get(tool.name),
            }
        )
    return entries
