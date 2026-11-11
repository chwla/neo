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

from dataclasses import dataclass, field
from typing import Any, Literal

from app.core.paths import path_key
from app.services.agent_core.tools.base import AgentTool
from app.services.agent_core.types import Grant, PermissionMode
from app.services.agent_framework.permissions import (
    DANGEROUS_TOOL_NAMES,
    can_access_path,
    clamp_permissions,
)
from app.services.agent_framework.types import AgentPermissions
from app.services.disclosure import Label
from app.services.isolation.launch import UNSANDBOXED_EXPOSURE
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

#: Tools whose arguments or effects leave the machine, or leave data somewhere an
#: external agent can later read. Reading private data is deliberately *not* on this
#: list -- using the user's data is what the internal agent is for; moving it outward
#: is what needs a decision.
#:
#: ``web_search``/``web_fetch`` send their arguments to a third party. A workspace
#: write is here because the next external run can read the workspace, which makes a
#: write into it a disclosure with a delay. Calendar and connector writes already ask
#: in every mode, so they need nothing extra.
#: Tools that bring the user's private data *into* a turn. Calling one raises the
#: turn's label, which is what later makes an outbound call ask for approval. The
#: workspace is not here: its own label decides that (see ``disclosure.artifacts``).
PRIVATE_READ_TOOLS: dict[str, tuple[str, ...]] = {
    "recall_memory": ("memory",),
    "search_gallery": ("gallery",),
    "view_image": ("gallery",),
    "list_calendar_events": ("calendar",),
    "create_calendar_event": ("calendar",),
    "update_calendar_event": ("calendar",),
    "delete_calendar_event": ("calendar",),
    "load_skill": ("skill",),
}

EGRESS_TOOLS: dict[str, str] = {
    "web_search": "a search provider",
    "web_fetch": "a website",
    "run_command": "a command that can send data over the network",
    "run_tests": "a test process that can send data over the network",
    "write_file": "this workspace, which an external agent can read",
    "edit_file": "this workspace, which an external agent can read",
    "delete_file": "this workspace, which an external agent can read",
    "create_checkpoint": "a workspace snapshot that can later be shared",
    "deliver_changes": "your own repository",
}

#: Shown when an edit to the user's own folder could not be undone afterwards.
UNRECOVERABLE_WRITE = (
    "Undo is not available for this folder on this computer, so this edit would be permanent."
)

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
    # Compared the way the filesystem will read it, not the way it was typed. The
    # file tools strip surrounding whitespace before opening, so " .env" opened
    # `.env` while a raw string comparison saw something else entirely; and on a
    # case-insensitive volume -- macOS and Windows by default -- ".ENV" opens the
    # same file as ".env". Both were ways to ask for a refused path and be given it.
    forbidden = {path_key(name) for name in HARD_FORBIDDEN}
    for segment in segments:
        cleaned = segment.strip().strip(".") if segment.strip() == "." else segment.strip()
        if not cleaned:
            continue
        if path_key(cleaned) in forbidden:
            return True
    return False


#: A run from before provenance was recorded: nothing says what it read.
LEGACY_CONTEXT_TAG = "legacy_context"

#: How each private source is named to the person being asked.
_SOURCE_NAMES = {
    "memory": "your memories",
    "gallery": "your gallery",
    "calendar": "your calendar",
    "skill": "an installed skill",
    "chat_private": "this conversation",
    "project": "this project",
    "agent_def": "this agent's instructions",
    "notes": "your notes",
    "files": "your files",
    LEGACY_CONTEXT_TAG: "earlier context whose source was not recorded",
}

#: Tools that only change the workspace: nothing leaves the machine when they run.
_WORKSPACE_ONLY_EGRESS = frozenset({"write_file", "edit_file", "delete_file"})


def _join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _normal_relative(value: str) -> list[str] | None:
    """A relative path as its segments, or ``None`` if it is not a plain one."""

    segments = [part for part in value.replace("\\", "/").split("/") if part not in {"", "."}]
    if not segments or ".." in segments:
        return None
    return [path_key(part) for part in segments]


def resolve_outcome(risk: str, mode: str) -> Outcome:
    """The overlay lookup, with an unknown combination failing closed."""

    return _OVERLAY.get(risk, {}).get(mode, "deny")  # type: ignore[return-value]


def tags_for_read(tool_name: str) -> tuple[str, ...]:
    """The private sources a tool call brings into the turn."""

    return PRIVATE_READ_TOOLS.get(tool_name, ())


@dataclass(frozen=True)
class EgressContext:
    """What private data this turn has already taken in.

    Carried into the decision rather than inferred from the arguments: by the time a
    model is composing a URL, whether the secret is in it is undecidable -- it could
    be encoded, split, or signalled by the choice of which URL to fetch. What *is*
    decidable is whether this turn ever read anything private, and that is what the
    label records.
    """

    label: Label = field(default_factory=lambda: Label.unknown("no_egress_context"))

    @property
    def restricted(self) -> bool:
        """Whether an outbound call needs the user's say-so."""

        return not self.label.is_public()

    def describe(self) -> str:
        """What was read, in words a person recognises -- never the internal tag."""

        if self.label.is_unknown():
            return "data of unrecorded origin"
        named = sorted({_SOURCE_NAMES.get(tag, "your private data") for tag in self.label.tags})
        return _join_words(named) or "your private data"

    @property
    def legacy_only(self) -> bool:
        """Restricted only because the run predates provenance records."""

        return self.label.is_private() and set(self.label.tags) == {LEGACY_CONTEXT_TAG}


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
        if not isinstance(value, str):
            return False
        # By whole path segments, compared the way the filesystem reads them. A
        # string prefix made a grant for ``src`` also cover ``src_private`` and
        # ``src/../secrets``.
        scope = _normal_relative(prefix)
        target = _normal_relative(value)
        if scope is None or target is None:
            return False
        return target[: len(scope)] == scope
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
        egress: EgressContext | None = None,
        isolated: bool = True,
        undo_available: bool = True,
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

        # 5. Egress. Checked before the mode overlay, because the mode's job is to
        #    say how freely the agent may act on the *workspace*, and this is a
        #    different question: may what this turn has read go outward at all.
        #    A run restricted only because it predates provenance records has no
        #    known private read behind it, so a write that stays in the workspace is
        #    left to the mode and the session's grants below.
        legacy_workspace_write = (
            egress is not None and egress.legacy_only and tool.name in _WORKSPACE_ONLY_EGRESS
        )
        if egress is not None and egress.restricted and not legacy_workspace_write and (
            tool.name in EGRESS_TOOLS or tool.source == "connector"
        ):
            destination = EGRESS_TOOLS.get(tool.name, "a connected service")
            reason = (
                f"This run has read {egress.describe()}, and {tool.name} sends data to "
                f"{destination}, so it needs your approval."
            )
            if tool.risk == "command" and not isolated:
                # The same card is the person's only chance to hear that the command
                # has no sandbox; without that text the launch layer will refuse it.
                reason = f"{reason} {UNSANDBOXED_EXPOSURE}"
            return PermissionDecision("ask", reason, grantable=False)

        # 6. Only now does the mode have a say, and only to narrow.
        outcome = resolve_outcome(tool.risk, mode)

        # 7. Without a sandbox a command runs with the user's whole account, so no
        #    mode runs one unattended: AUTO asks like NORMAL, no grant covers it, and
        #    the card carries the exposure the launch layer checks for.
        #    ``isolation.launch`` enforces the same rule again at the moment of launch.
        if tool.risk == "command" and not isolated and outcome in {"allow", "ask"}:
            return PermissionDecision("ask", UNSANDBOXED_EXPOSURE, grantable=False)

        # 8. A write the run cannot undo (a live folder on a platform with no safe
        #    restore) is not done unprompted. Unlike a command it may be granted for
        #    the session, because the predicate a person agrees to is a path.
        unrecoverable_write = tool.risk == "workspace_write" and (
            not undo_available or not tool.supports_undo
        )
        if unrecoverable_write and outcome == "allow":
            outcome = "ask"

        grantable = outcome == "ask" and tool.risk in _GRANTABLE_RISKS

        if outcome == "ask":
            for grant in grants or []:
                if grant_matches(grant, tool, arguments):
                    return PermissionDecision(
                        "allow", f"Covered by a session grant for {tool.name}.", via_grant=True
                    )

        reasons = {
            "allow": f"{tool.risk} is permitted in {mode} mode.",
            "ask": (
                f"{UNRECOVERABLE_WRITE} {tool.risk} requires approval in {mode} mode."
                if unrecoverable_write
                else f"{tool.risk} requires approval in {mode} mode."
            ),
            "deny": f"{tool.risk} is not permitted in {mode} mode.",
            "propose": "Plan mode proposes changes without making them.",
        }
        return PermissionDecision(outcome, reasons[outcome], grantable=grantable)


def is_grantable(tool: AgentTool) -> bool:
    return tool.risk in _GRANTABLE_RISKS
