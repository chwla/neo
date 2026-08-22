"""What an agent tool is.

Every tool is a thin adapter over a service Neo already has. No tool implements
a capability itself -- that is what keeps this package from becoming a second
copy of the file, git, search and sandbox layers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from app.services.agent_core.types import Evidence, ToolResult

# How dangerous an action is, independent of which service implements it. The
# permission resolver maps this onto the mode overlay; the six values line up
# with the categories already used by the connector tool registry.
RiskClass = Literal[
    "read",
    "workspace_write",
    "command",
    "external_write",
    "delivery",
    "forbidden",
]

#: Maps onto ``app.services.tools.types.ToolCategory`` so agent tools and
#: connector tools classify identically and share one approval story.
CATEGORY_FOR_RISK: dict[str, str] = {
    "read": "read_only",
    "workspace_write": "workspace_write_approval_required",
    "command": "workspace_write_approval_required",
    "external_write": "external_write_approval_required",
    "delivery": "external_write_approval_required",
    "forbidden": "dangerous_disabled",
}


@dataclass
class ToolContext:
    """Everything a tool may touch, passed explicitly rather than looked up."""

    session_id: str
    project_id: str | None = None
    repo_id: str | None = None
    task_id: str | None = None
    mode: str = "normal"
    extras: dict[str, Any] = field(default_factory=dict)


class ToolHandler(Protocol):
    def __call__(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult | str: ...


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: RiskClass
    handler: Callable[[dict[str, Any], ToolContext], Any]
    #: Argument keys holding a workspace-relative path, checked against the
    #: agent definition's allowed/forbidden file patterns before execution.
    path_arguments: tuple[str, ...] = ()
    #: A short sentence shown in the approval card, so the user reads intent
    #: rather than raw JSON.
    summary: Callable[[dict[str, Any]], str] | None = None
    #: Tools that cannot work without a managed repository are withheld from
    #: sessions that have none, rather than being offered and then failing.
    requires_repo: bool = False

    @property
    def category(self) -> str:
        return CATEGORY_FOR_RISK[self.risk]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def describe(self, arguments: dict[str, Any]) -> str:
        if self.summary is None:
            return self.name
        try:
            return self.summary(arguments)
        except Exception:
            return self.name

    def as_registry_dict(self) -> dict[str, Any]:
        """Shape ``tools.permissions.ensure_tool_allowed`` already understands."""

        return {
            "id": f"agent.{self.name}",
            "name": self.name,
            "category": self.category,
            "enabled": self.risk != "forbidden",
            "metadata": {"executor": f"agent.{self.name}"},
        }


def ok(call_id: str, name: str, content: str, evidence: Evidence | None = None) -> ToolResult:
    return ToolResult(call_id=call_id, name=name, content=content, evidence=evidence)


def error(call_id: str, name: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, name=name, status="error", content=message, error=message)
