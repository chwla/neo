"""Assembling the tool set for a session and dispatching calls.

The registry is where "which tools exist" and "which tools may run" are kept
apart. It only withholds a tool when offering it would be pointless -- there is
no repository, or the resolver denies it outright in every mode. Everything else
is advertised and gated at call time, so the model can discover that it needs
approval rather than silently lacking a capability.
"""

from __future__ import annotations

import time
from typing import Any

from app.services.agent_core.permissions import PermissionResolver
from app.services.agent_core.tools import (
    calendar,
    checkpoint,
    deliver,
    files,
    knowledge,
    shell,
    todo,
)
from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.types import PermissionMode, ToolCall, ToolResult
from app.services.agent_core.workspace import is_live

#: Tools that make no sense once the workspace is the user's own folder, mapped
#: to what the agent should understand instead. ``deliver_changes`` moved work
#: out of the managed copy, and there is no copy to move it out of. Checkpoints
#: committed inside that copy, and ``git/safety.py`` refuses -- correctly -- to
#: run git in the user's repository, so Neo never creates commits there.
LIVE_WITHHELD = {
    "deliver_changes": (
        "This workspace is the user's own folder, so your edits are already in it. "
        "There is nothing to deliver."
    ),
    "create_checkpoint": (
        "Neo does not create commits in the user's own repository. "
        "The run is journalled instead, and the user can undo it."
    ),
}

#: Output beyond this is elided in the middle: the head shows what happened and
#: the tail usually holds the error, while the middle is rarely load bearing.
MAX_TOOL_OUTPUT = 16_000


def default_tools() -> list[AgentTool]:
    return [
        *files.TOOLS,
        *shell.TOOLS,
        *knowledge.TOOLS,
        *todo.TOOLS,
        *checkpoint.TOOLS,
        *deliver.TOOLS,
        *calendar.TOOLS,
    ]


#: The 4 categories the per-chat Tools panel exposes. Toggling one off disables
#: every tool named here for that chat. Names absent here (todo_write,
#: create_checkpoint, deliver_changes) are ungrouped: always enabled, never
#: shown in that panel.
TOOL_GROUPS: dict[str, list[str]] = {
    "shell": ["run_command", "run_tests"],
    "file_operations": ["read_file", "list_dir", "write_file", "edit_file", "delete_file"],
    "search": ["glob", "grep", "find_symbol", "web_search", "web_fetch"],
    "memory": ["recall_memory"],
    "calendar": [
        "list_calendar_events",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    ],
}

#: Reverse lookup: tool name -> its group key, or absent if ungrouped.
TOOL_TO_GROUP: dict[str, str] = {
    name: group for group, names in TOOL_GROUPS.items() for name in names
}


def truncate_output(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    elided = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{elided} characters elided] ...\n\n{tail}"


class ToolRegistry:
    def __init__(self, tools: list[AgentTool] | None = None) -> None:
        self._tools = {
            tool.name: tool for tool in (tools if tools is not None else default_tools())
        }
        self.resolver = PermissionResolver()

    def get(self, name: str) -> AgentTool | None:
        return self._tools.get(name)

    def all(self) -> list[AgentTool]:
        return list(self._tools.values())

    def available(
        self,
        *,
        mode: PermissionMode,
        has_repo: bool,
        live: bool = False,
        disabled: frozenset[str] = frozenset(),
    ) -> list[AgentTool]:
        chosen = []
        for tool in self._tools.values():
            if tool.name in disabled:
                continue
            if tool.requires_repo and not has_repo:
                continue
            if live and tool.name in LIVE_WITHHELD:
                continue
            if self.resolver.resolve(tool=tool, arguments={}, mode=mode).outcome == "deny":
                continue
            chosen.append(tool)
        return chosen

    def schemas(
        self,
        *,
        mode: PermissionMode,
        has_repo: bool,
        live: bool = False,
        disabled: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        return [
            tool.schema()
            for tool in self.available(mode=mode, has_repo=has_repo, live=live, disabled=disabled)
        ]

    def execute(
        self, call: ToolCall, context: ToolContext, *, disabled: frozenset[str] = frozenset()
    ) -> ToolResult:
        """Run one call, turning any failure into a result the model can react to.

        Tool errors are returned rather than raised on purpose: "that path does
        not exist" is information the agent should use to try something else, and
        crashing the run would throw away everything done so far.
        """

        tool = self.get(call.name)
        if tool is None:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                status="error",
                content=f"No tool named '{call.name}' is available.",
                error="unknown tool",
            )

        # Withholding a tool from the schema is advice; a model can still name
        # one it saw in an earlier turn, or that it invented. Refusing here as
        # well is what makes it a rule.
        if call.name in disabled:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                status="denied",
                content="This tool has been turned off for this chat.",
                error="tool disabled for this chat",
            )

        if call.name in LIVE_WITHHELD and is_live(context.repo_id):
            return ToolResult(
                call_id=call.id,
                name=call.name,
                status="error",
                content=LIVE_WITHHELD[call.name],
                error="not available for a live workspace",
            )

        started = time.perf_counter()
        context.extras["call_id"] = call.id
        try:
            outcome = tool.handler(call.arguments, context)
        except Exception as exc:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                status="error",
                content=f"{type(exc).__name__}: {exc}",
                error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        duration = int((time.perf_counter() - started) * 1000)
        if isinstance(outcome, ToolResult):
            return outcome.model_copy(
                update={
                    "call_id": call.id,
                    "name": call.name,
                    "content": truncate_output(outcome.content),
                    "duration_ms": outcome.duration_ms or duration,
                }
            )
        return ToolResult(
            call_id=call.id,
            name=call.name,
            status="ok",
            content=truncate_output(str(outcome)),
            duration_ms=duration,
        )
