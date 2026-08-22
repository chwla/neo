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
from app.services.agent_core.tools import checkpoint, deliver, files, knowledge, shell, todo
from app.services.agent_core.tools.base import AgentTool, ToolContext
from app.services.agent_core.types import PermissionMode, ToolCall, ToolResult

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
    ]


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

    def available(self, *, mode: PermissionMode, has_repo: bool) -> list[AgentTool]:
        chosen = []
        for tool in self._tools.values():
            if tool.requires_repo and not has_repo:
                continue
            if self.resolver.resolve(tool=tool, arguments={}, mode=mode).outcome == "deny":
                continue
            chosen.append(tool)
        return chosen

    def schemas(self, *, mode: PermissionMode, has_repo: bool) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.available(mode=mode, has_repo=has_repo)]

    def execute(self, call: ToolCall, context: ToolContext) -> ToolResult:
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
