"""The canonical permission resolver.

Neo already had four places that decided what an agent may do: the runner's step
allowlist, the connector tool permissions, the agent-framework role clamps, and
the agentic-core policy. This module is deliberately *not* a fifth set of rules.
It imports the existing primitives, runs them first, and adds exactly one new
concept on top -- the PLAN/NORMAL/AUTO overlay.

The ordering matters and is the whole safety argument: the primitives run before
the overlay, so a mode can only ever narrow what they already permit. There is
no path through ``resolve`` that lets a mode widen anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.services.agent_core.tools.base import AgentTool
from app.services.agent_core.types import Grant, PermissionMode
from app.services.agent_framework.permissions import (
    DANGEROUS_TOOL_NAMES,
    can_access_path,
    clamp_permissions,
)
from app.services.agent_framework.types import AgentPermissions
from app.services.rules.safety import HARD_FORBIDDEN
from app.services.tools.permissions import (
    ToolPermissionError,
    ensure_tool_allowed,
    input_contains_credential,
    input_is_bounded,
)

Outcome = Literal["allow", "ask", "deny", "propose"]

# The one genuinely new rule in this module. Read across: PLAN never mutates,
# NORMAL asks before mutating, AUTO mutates inside the sandbox but still stops at
# its edge -- external writes and repository delivery are "ask" even here.
_OVERLAY: dict[str, dict[PermissionMode, Outcome]] = {
    "read": {"plan": "allow", "normal": "allow", "auto": "allow"},
    "workspace_write": {"plan": "propose", "normal": "ask", "auto": "allow"},
    "command": {"plan": "propose", "normal": "ask", "auto": "allow"},
    "external_write": {"plan": "deny", "normal": "ask", "auto": "ask"},
    "delivery": {"plan": "deny", "normal": "ask", "auto": "ask"},
    "forbidden": {"plan": "deny", "normal": "deny", "auto": "deny"},
}

# A session-level grant is only ever offered for edits inside the managed copy.
# Commands, external writes and delivery stay per-call: their arguments carry too
# much variation for any predicate a user could meaningfully agree to up front.
_GRANTABLE_RISKS = frozenset({"workspace_write"})


def _escapes_hard_forbidden(relative_path: str) -> bool:
    """Reject anything *inside* a hard-forbidden tree, not just the tree itself.

    ``can_access_path`` matches with ``fnmatch``, so the pattern ``.git`` stops
    ``.git`` but not ``.git/config``. Reading a file is one thing; an agent that
    can write needs the whole subtree closed, so the segments are checked too.
    """

    normalized = relative_path.replace("\\", "/").strip("/")
    if not normalized:
        return True
    segments = normalized.split("/")
    if ".." in segments:
        return True
    return any(segment in HARD_FORBIDDEN for segment in segments)


def resolve_outcome(risk: str, mode: str) -> Outcome:
    """The overlay lookup, with an unknown combination failing closed."""

    return _OVERLAY.get(risk, {}).get(mode, "deny")  # type: ignore[return-value]


@dataclass(frozen=True)
class PermissionDecision:
    outcome: Outcome
    reason: str
    grantable: bool = False
    via_grant: bool = False

    @property
    def allowed(self) -> bool:
        return self.outcome == "allow"


def grant_matches(grant: Grant, tool: AgentTool, arguments: dict[str, Any]) -> bool:
    """Re-evaluate a grant's predicate against the arguments actually being run.

    A grant never generalises. It is checked here, at execution time, against
    real values -- which is what stops "allow always" from becoming a standing
    permission for arguments the user never saw.
    """

    if grant.tool_name != tool.name:
        return False
    predicate = grant.predicate or {}
    kind = predicate.get("kind")
    if kind == "any":
        return True
    if kind == "path_prefix":
        argument = str(predicate.get("argument") or "path")
        prefix = str(predicate.get("value") or "")
        value = arguments.get(argument)
        return isinstance(value, str) and bool(prefix) and value.startswith(prefix)
    if kind == "exact_arguments":
        return dict(predicate.get("value") or {}) == dict(arguments)
    # An unrecognised predicate grants nothing.
    return False


class PermissionResolver:
    """Resolves one tool call against the existing guards, then the mode overlay."""

    def resolve(
        self,
        *,
        tool: AgentTool,
        arguments: dict[str, Any],
        mode: PermissionMode,
        grants: list[Grant] | None = None,
        permissions: AgentPermissions | None = None,
    ) -> PermissionDecision:
        # 1. The denylist that predates modes, and cannot be argued with.
        if tool.name in DANGEROUS_TOOL_NAMES or tool.risk == "forbidden":
            return PermissionDecision("deny", f"'{tool.name}' is not available to agents.")

        # 2. Input shape guards, reused verbatim from the connector tool layer.
        if not input_is_bounded(arguments):
            return PermissionDecision("deny", "Tool input is too large.")
        if input_contains_credential(arguments):
            return PermissionDecision(
                "deny", "Tool inputs may not contain credentials; use the connector vault."
            )

        # 3. The connector permission gate, which also enforces enabled/category.
        try:
            ensure_tool_allowed(
                tool=tool.as_registry_dict(),
                server=None,
                agent=None,
                skill=None,
                input_payload=arguments,
            )
        except ToolPermissionError as exc:
            return PermissionDecision("deny", str(exc))

        # 4. Path scoping. ``clamp_permissions`` is what unions HARD_FORBIDDEN into
        #    the pattern list, so it runs even when the caller passed nothing.
        effective, _warnings = clamp_permissions(permissions or AgentPermissions())
        for key in tool.path_arguments:
            value = arguments.get(key)
            if not isinstance(value, str) or not value:
                continue
            if _escapes_hard_forbidden(value) or not can_access_path(effective, value):
                return PermissionDecision("deny", f"Path '{value}' is outside this agent's scope.")

        # 5. Only now does the mode have a say, and only to narrow.
        outcome = resolve_outcome(tool.risk, mode)
        grantable = outcome == "ask" and tool.risk in _GRANTABLE_RISKS

        if outcome == "ask":
            for grant in grants or []:
                if grant_matches(grant, tool, arguments):
                    return PermissionDecision(
                        "allow", f"Covered by a session grant for {tool.name}.", via_grant=True
                    )

        reasons = {
            "allow": f"{tool.risk} is permitted in {mode} mode.",
            "ask": f"{tool.risk} requires approval in {mode} mode.",
            "deny": f"{tool.risk} is not permitted in {mode} mode.",
            "propose": "Plan mode proposes changes without making them.",
        }
        return PermissionDecision(outcome, reasons[outcome], grantable=grantable)


def is_grantable(tool: AgentTool) -> bool:
    return tool.risk in _GRANTABLE_RISKS
