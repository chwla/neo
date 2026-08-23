"""Bridges enabled connector tool definitions into the agent loop's tool set.

Neo already has a separate "connector tools" system (``app.services.tools``)
for user-defined MCP/REST tools, with its own store, approval queue and
Settings UI. This module is the one place that turns its rows into
``AgentTool`` objects the agent loop can actually call -- the handler here is
a thin adapter over ``execute_tool``, the same function the connector
system's own approval-queue path calls, so there is exactly one executor.
Risk still flows through ``PermissionResolver``'s mode overlay, same as any
built-in tool; this deliberately does not touch the connector system's own
``request_call``/``approval_required`` queue, which would be a second,
competing approval mechanism for the same call.
"""

from __future__ import annotations

import json

from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.tools import store as tools_store
from app.services.tools.executor import ToolValidationError, execute_tool

#: Neo's own seeded connector stubs (repo_metadata, summarize_text, the
#: create_note no-op) duplicate capability agent_core already has under other
#: names. Bridging them would just be noise in every chat's tool list.
_EXCLUDED_SERVER_IDS = {"builtin-neo"}

#: ``CATEGORY_FOR_RISK`` in ``tools/base.py`` is many-to-one, so there is no
#: lossless auto-invert. ``dangerous_disabled`` is intentionally absent --
#: those rows are filtered out before this mapping is consulted.
_CATEGORY_TO_RISK: dict[str, str] = {
    "read_only": "read",
    "workspace_read": "read",
    "external_read": "read",
    "workspace_write_approval_required": "workspace_write",
    "external_write_approval_required": "external_write",
}


def _make_handler(tool_id: str):
    def handler(arguments: dict, context: ToolContext) -> str:
        del context
        tool = tools_store.get_tool(tool_id)
        if tool is None or not tool.get("enabled"):
            raise ToolValidationError("This tool is no longer available.")
        server = tools_store.get_server(tool["server_id"]) if tool.get("server_id") else None
        return json.dumps(execute_tool(tool, server, arguments))

    return handler


def bridged_connector_tools(existing_names: set[str]) -> list[AgentTool]:
    """Enabled connector tools, adapted for the agent loop.

    ``existing_names`` is the set of built-in tool names already claimed; a
    connector tool sharing one is skipped rather than shadowing it, and any
    name this function does add is folded back in so a later call in the same
    merge cannot collide either.
    """

    # The connector store's tables are created lazily by ``ToolsService``, which
    # a session run never otherwise constructs. A fresh profile that has never
    # opened Settings -> Tools & Skills would 500 on ``list_tools`` without this.
    tools_store.initialize_tool_tables()

    tools: list[AgentTool] = []
    for row in tools_store.list_tools(include_disabled=False):
        if row.get("server_id") in _EXCLUDED_SERVER_IDS:
            continue
        risk = _CATEGORY_TO_RISK.get(row["category"])
        if risk is None:
            continue
        if row["name"] in existing_names:
            continue
        server = tools_store.get_server(row["server_id"]) if row.get("server_id") else None
        if server is not None and not server.get("enabled"):
            continue
        tools.append(
            AgentTool(
                name=row["name"],
                description=row.get("description") or row.get("display_name") or row["name"],
                parameters=row.get("input_schema") or {"type": "object", "properties": {}},
                risk=risk,
                handler=_make_handler(row["id"]),
                source="connector",
                origin_id=row["id"],
            )
        )
        existing_names.add(row["name"])
    return tools


def chat_tools_catalog(registry, disabled: set[str]) -> list[dict]:
    """The full candidate tool set for a chat's Tools panel, toggle-annotated."""

    entries = []
    for tool in registry.all():
        entry = {
            "name": tool.name,
            "display_name": tool.name,
            "description": tool.description,
            "risk": tool.risk,
            "category": tool.category,
            "requires_repo": tool.requires_repo,
            "source": tool.source,
            "enabled": tool.name not in disabled,
        }
        if tool.source == "connector" and tool.origin_id:
            row = tools_store.get_tool(tool.origin_id)
            if row:
                entry["display_name"] = row.get("display_name") or row["name"]
                entry["tool_id"] = row["id"]
                server = tools_store.get_server(row["server_id"]) if row.get("server_id") else None
                entry["server_name"] = server.get("name") if server else None
        entries.append(entry)
    return entries
