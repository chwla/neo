"""Builds the per-chat tool catalog (the Tools panel's toggle-annotated list).

This used to also bridge Neo's connector-tool system (user-added MCP/REST
tools) into the agent loop's tool set. That system has been removed --
``chat_tools_catalog`` now only ever sees built-in tools.
"""

from __future__ import annotations

from app.services.agent_core.tools.registry import TOOL_TO_GROUP


def chat_tools_catalog(registry, disabled: set[str]) -> list[dict]:
    """The full candidate tool set for a chat's Tools panel, toggle-annotated."""

    entries = []
    for tool in registry.all():
        entries.append(
            {
                "name": tool.name,
                "display_name": tool.name,
                "description": tool.description,
                "risk": tool.risk,
                "category": tool.category,
                "requires_repo": tool.requires_repo,
                "source": tool.source,
                "enabled": tool.name not in disabled,
                "group": TOOL_TO_GROUP.get(tool.name),
            }
        )
    return entries
