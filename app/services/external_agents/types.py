"""Contracts shared by the external-executor layer.

The layer's whole job is turning another program's event stream into Neo's. So
the pivot is :class:`ExternalEvent`: an adapter is a pure function from one line
of a CLI's JSONL to zero or more of these, and the runner is the only thing that
knows how to persist one. That split is what lets the adapters be tested against
recorded fixtures with neither binary installed -- which matters, because the
binaries are the part of this system we do not control.

An ``ExternalEvent`` carries two different kinds of thing at once:

* ``type``/``payload`` -- an event to append to the session log, using the
  vocabulary in ``agent_core.events``. No new streaming protocol.
* the side-channel fields (``external_session_id``, ``final_text``, ``meta``,
  ``outcome``) -- facts the runner records on the session row rather than
  streams. They ride along here because they arrive interleaved with the
  events, and threading a second return channel through every adapter would
  buy nothing.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: How a run ended, from the adapter's point of view. Deliberately smaller than
#: Neo's ``StopReason``: an external CLI reports success or failure, and Neo
#: decides what that means for the session.
ExternalOutcome = Literal["completed", "failed"]


class ExecutorCapabilities(BaseModel):
    """What one external CLI can actually do.

    One typed record rather than booleans scattered across argv builders and
    `if executor == ...` branches. Every field is a claim about a *measured*
    CLI surface -- the evidence for each is in
    ``docs/external-agents/cli-surface.md`` -- and the rule is that a capability
    is false unless the CLI genuinely provides it. Reporting a capability Neo
    cannot deliver is worse than reporting none, because the interface then
    implies an enforcement that is not happening.

    ``model_config`` forbids extras so a typo becomes an error at import rather
    than a silently-absent capability that reads as ``False``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Continue this CLI's own earlier conversation rather than starting a new
    #: one. Claude Code: ``--resume <uuid>``. Codex: ``codex exec resume <id>``.
    resume: bool = False
    #: A read-only/planning mode that will not modify the repository. Note this
    #: is *repository* protection, not filesystem isolation -- Claude Code still
    #: writes its own plan files outside the repo.
    plan_mode: bool = False
    #: Neo can genuinely withhold named tools. Claude Code:
    #: ``--disallowedTools``. Codex has no per-tool flag at all.
    tool_denylist: bool = False
    #: Neo can pause the CLI and approve one tool call at a time. **Neither CLI
    #: offers this non-interactively today**: Claude Code 2.1.258 has no
    #: ``--permission-prompt-tool``, and ``codex exec`` rejects
    #: ``--ask-for-approval``. Kept as a field because it is the single most
    #: important thing for the interface to state honestly.
    per_tool_approval: bool = False
    #: The CLI reports a monetary cost Neo may display. Claude Code emits
    #: ``total_cost_usd``; Codex reports none, and Neo must not invent one.
    cost_reporting: bool = False
    #: The CLI reports token usage Neo may display.
    token_reporting: bool = False


class ExecutorSpec(BaseModel):
    """Static facts about one external CLI.

    Everything here is knowable without running anything, so detection, argv
    building and the UI can all read the same description.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    #: Settings attribute holding an explicit path to the binary ("" = use PATH).
    bin_setting: str
    #: The name to look for on PATH when no explicit path is configured.
    program: str
    #: Settings attribute holding the CLI's own config/credential directory,
    #: when a deployment explicitly overrides it. Empty means "say nothing".
    home_setting: str
    #: Environment variable through which that directory would be passed. Only
    #: ever set when ``home_setting`` names a non-empty value -- see ``env``.
    home_env: str
    #: The key this CLI's conversation id is recorded under in the session's
    #: per-executor metadata. Codex calls it a thread; Claude Code calls it a
    #: session. Held here so nothing else has to branch on the engine to
    #: read or write it.
    session_id_key: str = "session_id"
    capabilities: ExecutorCapabilities = Field(default_factory=ExecutorCapabilities)


class InvocationContext(BaseModel):
    """Everything an adapter needs to build one command line.

    Uniform across executors so the service layer dispatches through a table
    rather than an ``if executor == ...`` chain. Fields an executor does not use
    are simply ignored by it -- Claude Code has no ``cwd`` flag because the
    process cwd is enough, and Codex has no id to assign.
    """

    model_config = ConfigDict(frozen=True)

    binary: str
    prompt: str
    mode: str
    cwd: str
    resume_id: str | None = None
    #: An id Neo has minted, for a CLI that lets us assign one.
    new_session_id: str = ""
    disabled_tools: list[str] = Field(default_factory=list)
    model: str | None = None
    unsafe: bool = False


class Invocation(BaseModel):
    """What an adapter decided to run, and how to read what comes back."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    argv: list[str]
    #: One parsed JSONL line -> zero or more Neo events.
    translate: Any
    #: The conversation id, when this CLI let Neo choose it up front.
    session_id: str | None = None


class ExternalEvent(BaseModel):
    """One thing that happened inside an external run."""

    #: An event name from ``agent_core.events``. Empty means "record the
    #: side-channel fields but append nothing" -- used for events that carry
    #: only metadata, such as Claude Code's rate-limit notice.
    type: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    #: The CLI's own conversation id, when it first becomes known.
    external_session_id: str | None = None
    #: The run's answer. Several may arrive; the last one wins, because both
    #: CLIs emit intermediate narration before the final response.
    final_text: str | None = None
    #: Usage, cost, model -- merged into the session's per-executor metadata.
    meta: dict[str, Any] = Field(default_factory=dict)
    #: Set only by a terminal event.
    outcome: ExternalOutcome | None = None
    #: Why it failed, when it did.
    error: str | None = None


class RunOutcome(BaseModel):
    """What one external process run produced, once it is over."""

    outcome: ExternalOutcome = "failed"
    final_text: str = ""
    external_session_id: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    exit_code: int | None = None
    error: str | None = None
    #: True when the process was killed for exceeding its wall clock.
    timed_out: bool = False
    #: True when the process was killed because the session was cancelled.
    cancelled: bool = False


class HandoffStep(BaseModel):
    """One executor's turn inside a chain."""

    executor: str
    mode: str = "normal"
    #: Shown in the transcript divider, e.g. "Plan" or "Review".
    role: str = ""
    #: Prepended to the objective for this step. The previous step's answer and
    #: the change summary are appended by ``chain`` rather than templated here,
    #: so a preset stays readable.
    instructions: str = ""


class ExternalAgentError(RuntimeError):
    """The run could not be started, or could not be started safely."""


__all__ = [
    "ExecutorCapabilities",
    "ExecutorSpec",
    "Invocation",
    "InvocationContext",
    "ExternalAgentError",
    "ExternalEvent",
    "ExternalOutcome",
    "HandoffStep",
    "RunOutcome",
]
