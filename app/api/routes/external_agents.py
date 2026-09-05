"""What external executors exist, whether they can be used, and how to fix it.

Two audiences, and the split between them is the reason there are two listing
endpoints. The composer asks ``/external-agents`` for what it may offer, and
offers only what is usable: an engine you have not signed in to is not a choice
you are declining to make, it is a task, and a dropdown is a bad place to put a
task. Settings asks ``/external-agents/setup`` for the fuller picture, because
that is where the task belongs and answering "why is it not there?" needs facts
the composer's gated view deliberately hides.

That second view is why a row carries a ``reason`` rather than only a flag:
"unavailable" sends someone looking in the wrong place, while "not signed in --
run `codex login`" is a fix. And a reason is only half of one, so the other half
is here too. Every state an engine can be unavailable in has an endpoint that
resolves it from the panel that reported it: ``/enable`` for the feature being
off, ``/{executor}/connect`` and ``/{executor}/login`` for a CLI that is signed
out, and ``?refresh=true`` for a cached answer that has since stopped being
true. The only state left without a button is a CLI that is not installed,
because installing it is not Neo's to do.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import chat_prefs
from app.services.external_agents import chain as external_chain
from app.services.external_agents import detect, login
from app.services.external_agents.types import ExternalAgentError

router = APIRouter(tags=["external-agents"])


#: The sentence the interface has to be able to say. External executors run
#: under *their own* permission and sandbox model; Neo starts them, watches them
#: and can stop them, but it does not authorise their individual tool calls.
#: Returned from the API rather than written into the frontend so the claim has
#: exactly one source.
TRUST_BOUNDARY = {
    "neo_controls": [
        "which folder the agent runs in, re-validated on every run",
        "starting, watching and stopping the run",
        "recording what changed, and undoing it",
        "which environment variables the process can see",
    ],
    "cli_controls": [
        "which tools it uses, and when",
        "approving or refusing its own individual actions",
        "its own sandbox and permission model",
        "its own credentials and authentication",
    ],
    "summary": (
        "This engine runs through its own CLI permission and sandbox model. "
        "Neo starts and stops the run and records what it changed, but cannot "
        "approve individual tool calls inside it."
    ),
}


def _describe(executor: dict[str, Any]) -> dict[str, Any]:
    """Annotate one executor with what Neo can and cannot enforce for it.

    Stated per executor rather than once, because the answer differs: Neo can
    withhold named tools from Claude Code and cannot from Codex, and a UI that
    showed one control for both would be lying about one of them.
    """

    capabilities = executor.get("capabilities") or {}
    notes: list[str] = []
    if not capabilities.get("per_tool_approval"):
        notes.append("Neo cannot approve individual tool calls for this engine.")
    if not capabilities.get("tool_denylist"):
        notes.append("Per-chat tool toggles do not apply to this engine.")
    if capabilities.get("plan_mode"):
        # Precise on purpose. Plan mode reliably protects the repository; it is
        # not filesystem isolation, and describing it as a sandbox would be a
        # stronger claim than the CLI supports.
        notes.append("Plan mode prevents repository changes, but is not full filesystem isolation.")
    if not capabilities.get("cost_reporting"):
        notes.append("This engine does not report cost.")
    return {**executor, "notes": notes}


def _blocker(row: dict[str, Any], *, enabled: bool) -> str:
    """Which of the four unavailable-shaped things this is.

    Returned as a token beside the prose reason so the interface can offer the
    matching action -- turn it on, sign in, install it -- without matching on
    English. The prose stays the thing a person reads; this is the thing the
    frontend branches on, and they cannot drift apart because both come from
    the same row.
    """

    # The gate is checked first, and that ordering is the point. On the setup
    # endpoint a row can be a *machine* fact -- installed, signed in, ready --
    # while the profile has still not opted in, and the action that unblocks it
    # is "turn it on", not "nothing". Reporting readiness there would offer no
    # button at all for the one thing standing in the way.
    if not enabled:
        return "disabled"
    if row.get("available"):
        return "none"
    if not row.get("version"):
        return "not_installed"
    if not row.get("auth"):
        return "signed_out"
    return "unknown"


def _row(row: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    described = _describe(row)
    return {
        **described,
        "blocker": _blocker(row, enabled=enabled),
        "command": login.LOGIN_COMMAND.get(row.get("id", ""), ""),
    }


@router.get("/external-agents")
def list_external_agents(
    refresh: bool = Query(default=False, description="Re-probe instead of using the cache."),
) -> dict[str, Any]:
    """Executors and handoff presets, with availability and the trust boundary.

    Gated: while the feature is off for this profile nothing is spawned, and
    every engine comes back with ``blocker: "disabled"``. The setup endpoint is
    the one that probes regardless, because answering "why?" requires it.
    """

    enabled = chat_prefs.external_agents_enabled()
    executors = [_row(row, enabled=enabled) for row in detect.statuses(refresh=refresh)]
    presets = []
    for preset in external_chain.describe_presets():
        usable, reason = external_chain.available(preset["id"])
        presets.append({**preset, "available": usable, "reason": reason})
    return {
        "enabled": enabled,
        "executors": executors,
        "handoffs": presets,
        "trust_boundary": TRUST_BOUNDARY,
    }


@router.get("/external-agents/setup")
def external_agent_setup(
    refresh: bool = Query(default=False, description="Re-probe instead of using the cache."),
) -> dict[str, Any]:
    """Everything Settings > Engines needs, including facts the gate normally hides.

    This deliberately probes even when the feature is off. The listing above
    cannot: it is called on every composer load, and a disabled profile should
    not be spawning CLI probes in the background. Here the probe *is* the
    question being asked -- someone opened the panel to set an engine up -- and
    "installed, signed in, just not switched on" is an answer only an ungated
    probe can give, and the one that decides whether the button says "Turn on"
    or sends someone to a sign-in they have already done.

    So ``available`` on these rows means the *machine* fact -- installed and
    signed in -- while ``blocker`` accounts for the profile switch as well. The
    two differ exactly in the case the panel exists to explain.
    """

    enabled = chat_prefs.external_agents_enabled()
    rows = [_row(row, enabled=enabled) for row in detect.inspections(refresh=refresh)]
    return {
        "enabled": enabled,
        "executors": rows,
        "logins": login.states(),
        "trust_boundary": TRUST_BOUNDARY,
    }


class EnableRequest(BaseModel):
    enabled: bool = Field(description="Whether this profile may run external engines.")


@router.post("/external-agents/enable")
def set_external_agents_enabled(request: EnableRequest) -> dict[str, Any]:
    """Turn external engines on or off for this profile.

    An opt-in, still: the default remains off and this records a deliberate
    choice. What it stops being is an undiscoverable one -- the person who was
    told an engine is unavailable can now act on that where they read it,
    instead of being sent to find an environment variable.

    The detection cache is dropped on the way out. It was populated under the
    old answer, and the next question is being asked under the new one.
    """

    enabled = chat_prefs.set_external_agents_enabled(request.enabled)
    detect.clear_cache()
    if not enabled:
        # Nothing half-started should outlive the switch being turned off.
        login.reset()
    rows = [_row(row, enabled=enabled) for row in detect.statuses(refresh=enabled)]
    return {"enabled": enabled, "executors": rows}


@router.post("/external-agents/{executor}/connect")
def connect_executor(executor: str) -> dict[str, Any]:
    """Do whatever it takes to make this engine usable, and say where it got to.

    One call, because every step it performs is a step the person setting an
    engine up did not ask to think about. Pressing "Sign in" next to Claude Code
    means "let me run turns on Claude Code"; it does not mean "first consent to
    a feature flag, then re-probe, then start a sign-in". Those still happen --
    they just happen here rather than as three things to click.

    **Connecting the engine is the opt-in.** The feature stays off by default
    and is still only ever turned on by a deliberate act; this is that act, made
    in Settings where the consequences are written down beside it. The trust
    boundary is reported here and stated by the panel -- informing someone is
    not the same as making them click twice.

    Returns a ``state`` the interface can branch on directly:

    * ``ready`` -- usable now; it is offered in the engine picker,
    * ``signing_in`` -- the CLI's own sign-in has started; poll the login,
    * ``not_installed`` -- nothing Neo can do; say what to install,
    * ``error`` -- it could not be started, with the reason.
    """

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")

    chat_prefs.set_external_agents_enabled(True)
    detect.clear_cache()
    row = detect.inspect(executor, refresh=True)
    described = _row(row, enabled=True)

    if row.get("available"):
        return {"state": "ready", "engine": described, "login": login.state(executor)}
    if not row.get("version"):
        return {"state": "not_installed", "engine": described, "login": login.state(executor)}

    try:
        started = login.start(executor)
    except ExternalAgentError as error:
        return {
            "state": "error",
            "engine": described,
            "login": login.state(executor),
            "error": str(error),
        }
    return {"state": "signing_in", "engine": described, "login": started}


@router.post("/external-agents/{executor}/login")
def start_login(executor: str) -> dict[str, Any]:
    """Start the CLI's own sign-in and return what it is saying so far."""

    try:
        return login.start(executor)
    except ExternalAgentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/external-agents/{executor}/login")
def login_state(executor: str) -> dict[str, Any]:
    """Poll an in-flight sign-in: its URL, whether it wants a code, how it ended."""

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")
    return login.state(executor)


class LoginCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


@router.post("/external-agents/{executor}/login/code")
def submit_login_code(executor: str, request: LoginCodeRequest) -> dict[str, Any]:
    """Forward the code from the sign-in page to the waiting CLI.

    Claude Code's browser flow ends on a page showing a code rather than on a
    local callback, so this is the one piece of the exchange that has to travel
    back through Neo. It goes to the child's stdin and is stored nowhere.
    """

    try:
        return login.submit_code(executor, request.code)
    except ExternalAgentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/external-agents/{executor}/login")
def cancel_login(executor: str) -> dict[str, Any]:
    """Abandon a sign-in, killing the CLI and freeing its callback port."""

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")
    return login.cancel(executor)
