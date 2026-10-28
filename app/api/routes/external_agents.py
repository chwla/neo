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

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.routes.accounts import SESSION_COOKIE, session_for
from app.services import chat_prefs
from app.services.disclosure import approvals as disclosure_approvals
from app.services.external_agents import chain as external_chain
from app.services.external_agents import detect, login
from app.services.external_agents import models as external_models
from app.services.external_agents import usage as external_usage
from app.services.external_agents.types import ExternalAgentError

router = APIRouter(tags=["external-agents"])


class DisclosurePreviewRequest(BaseModel):
    chat_id: int = Field(gt=0)
    request_id: str = Field(min_length=8, max_length=80)
    engine: Literal["claude_code", "codex", "cursor", "antigravity"]
    objective: str = Field(min_length=1, max_length=20_000)
    source_message_id: int | None = Field(default=None, gt=0)
    source_note_id: str | None = Field(default=None, min_length=1, max_length=100)


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


#: Fields of an engine row that describe the *vendor account* the CLI is signed in
#: as -- not the engine, the person. They are the user's own identity, read out of
#: their CLI's credential store, and a guest session has no claim on them.
_ACCOUNT_FIELDS = ("email", "organization", "plan", "auth_method")


def _require_account(request: Request) -> dict:
    """Refuse a guest session.

    A guest is created by asking: no password, nothing to prove. Running a
    credentialed coding CLI spends the real account's subscription, acts as them
    with the vendor, and edits their files, so it is not a privilege a guest gets.
    """

    profile = session_for(request)
    if profile is None:
        raise HTTPException(status_code=401, detail="authenticated_profile_required")
    if profile.get("is_guest"):
        raise HTTPException(
            status_code=403,
            detail=(
                "External engines need a profile of your own. A guest session cannot "
                "run them or see which account a CLI is signed in to."
            ),
        )
    return profile


def _is_guest(request: Request) -> bool:
    profile = session_for(request)
    return bool(profile and profile.get("is_guest"))


def _redact_account(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, **{field: None for field in _ACCOUNT_FIELDS if field in row}}


def _row(row: dict[str, Any], *, enabled: bool) -> dict[str, Any]:
    described = _describe(row)
    return {
        **described,
        "blocker": _blocker(row, enabled=enabled),
        "command": login.LOGIN_COMMAND.get(row.get("id", ""), ""),
    }


@router.get("/external-agents")
def list_external_agents(
    request: Request,
    refresh: bool = Query(default=False, description="Re-probe instead of using the cache."),
) -> dict[str, Any]:
    """Executors and handoff presets, with availability and the trust boundary.

    Gated: while the feature is off for this profile nothing is spawned, and
    every engine comes back with ``blocker: "disabled"``. The setup endpoint is
    the one that probes regardless, because answering "why?" requires it.
    """

    enabled = chat_prefs.external_agents_enabled()
    # A guest still sees which engines exist -- the composer needs that to explain
    # itself -- but not whose vendor account they are signed in to.
    guest = _is_guest(request)
    executors = [
        _redact_account(_row(row, enabled=enabled)) if guest else _row(row, enabled=enabled)
        for row in detect.statuses(refresh=refresh)
    ]
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
    request: Request,
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

    # Setting an engine up is an account's job, and this endpoint is the one that
    # names the signed-in vendor account, so a guest gets no further.
    _require_account(request)
    from app.services.external_agents import compat

    enabled = chat_prefs.external_agents_enabled()
    # Whether this engine has passed its check here. The panel needs it to offer the
    # check, and without it an installed engine would look ready and then refuse.
    rows = [
        {**_row(row, enabled=enabled), "validated": compat.load(row.get("id", "")) is not None}
        for row in detect.inspections(refresh=refresh)
    ]
    return {
        "enabled": enabled,
        "executors": rows,
        "logins": login.states(),
        "trust_boundary": TRUST_BOUNDARY,
    }


@router.get("/external-agents/usage")
def external_agent_usage(
    refresh: bool = Query(default=False, description="Re-read instead of using the cache."),
) -> dict[str, Any]:
    """How much of each engine's subscription window is spent.

    Gated like the composer listing above and for the same reason: it is loaded
    from the composer, and a profile that has not opted into external engines
    should not have its disk walked for them. A disabled profile gets rows whose
    ``reason`` says so, exactly as the listing does.

    Every row states *when* its figure was observed and which source it came
    from, because none of them is live -- see ``external_agents.usage``. Callers
    are expected to show both; a percentage without an "as of" reads as current
    when it may be days old.
    """

    return {
        "enabled": chat_prefs.external_agents_enabled(),
        "executors": external_usage.snapshots(refresh=refresh),
        "warning_percent": external_usage.WARNING_PERCENT,
    }


@router.get("/external-agents/{executor}/models")
def executor_models(
    executor: str,
    refresh: bool = Query(default=False, description="Re-probe instead of using the cache."),
) -> dict[str, Any]:
    """Which models this engine can be asked for, and where each answer came from.

    Deliberately short, and sometimes empty. Neither CLI enumerates the models
    an account holds -- there is no subcommand for it, and an unknown model is
    rejected without naming the known ones -- so this reports only measured
    facts: the model the CLI's own config names, the shorthand its own ``--help``
    documents, and every model id Neo has watched a run report. Padding that out
    with a plausible-looking catalogue would be Neo asserting an account has
    models it may not, and going stale the first time a vendor ships one.
    """

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")
    return external_models.catalogue(executor, refresh=refresh)


class EnableRequest(BaseModel):
    enabled: bool = Field(description="Whether this profile may run external engines.")


@router.post("/external-agents/enable")
def set_external_agents_enabled(request: EnableRequest, http_request: Request) -> dict[str, Any]:
    """Turn external engines on or off for this profile.

    An opt-in, still: the default remains off and this records a deliberate
    choice. What it stops being is an undiscoverable one -- the person who was
    told an engine is unavailable can now act on that where they read it,
    instead of being sent to find an environment variable.

    The detection cache is dropped on the way out. It was populated under the
    old answer, and the next question is being asked under the new one.
    """

    _require_account(http_request)

    enabled = chat_prefs.set_external_agents_enabled(request.enabled)
    detect.clear_cache()
    external_models.clear_cache()
    external_usage.clear_cache()
    if not enabled:
        # Nothing half-started should outlive the switch being turned off.
        login.reset()
    rows = [_row(row, enabled=enabled) for row in detect.statuses(refresh=enabled)]
    return {"enabled": enabled, "executors": rows}


@router.post("/external-agents/{executor}/connect")
def connect_executor(executor: str, request: Request) -> dict[str, Any]:
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

    _require_account(request)

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


@router.post("/external-agents/{executor}/validate")
def validate_executor(executor: str, request: Request) -> dict[str, Any]:
    """Check one engine here, once, and record the result.

    Runs the engine on a throwaway repository with a fixed, harmless instruction --
    write one file -- under the same isolation a real run gets. It is the only way
    to know that this engine, as installed on this machine, authenticates and takes
    its prompt from stdin; without a pass, runs are refused rather than attempted.
    """

    _require_account(request)
    from app.services.external_agents import validation

    try:
        result = validation.validate(executor)
    except ExternalAgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "executor": result.executor,
        "validated": result.ok,
        "detail": result.detail,
        "checks": result.checks,
    }


@router.get("/external-agents/compat")
def external_agent_compat(request: Request) -> dict[str, Any]:
    """Which engines have been checked here, and when."""

    _require_account(request)
    from app.services.external_agents import compat

    return {"engines": [compat.describe(name) for name in detect.SPECS]}


@router.post("/external-agents/{executor}/login")
def start_login(executor: str, request: Request) -> dict[str, Any]:
    """Start the CLI's own sign-in and return what it is saying so far."""

    _require_account(request)

    try:
        return login.start(executor)
    except ExternalAgentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.get("/external-agents/{executor}/login")
def login_state(executor: str, request: Request) -> dict[str, Any]:
    """Poll an in-flight sign-in: its URL, whether it wants a code, how it ended."""

    _require_account(request)

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")
    return login.state(executor)


class LoginCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


@router.post("/external-agents/{executor}/login/code")
def submit_login_code(
    executor: str, request: LoginCodeRequest, http_request: Request
) -> dict[str, Any]:
    """Forward the code from the sign-in page to the waiting CLI.

    Claude Code's browser flow ends on a page showing a code rather than on a
    local callback, so this is the one piece of the exchange that has to travel
    back through Neo. It goes to the child's stdin and is stored nowhere.
    """

    _require_account(http_request)

    try:
        return login.submit_code(executor, request.code)
    except ExternalAgentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/external-agents/{executor}/login")
def cancel_login(executor: str, request: Request) -> dict[str, Any]:
    """Abandon a sign-in, killing the CLI and freeing its callback port."""

    _require_account(request)

    if executor not in detect.SPECS:
        raise HTTPException(status_code=404, detail=f"Unknown engine '{executor}'.")
    return login.cancel(executor)


def _disclosure_profile(request: Request) -> str:
    profile = _require_account(request)
    return str(profile["id"])


def _require_disclosure_origin(request: Request) -> None:
    """Grant requests require a real same-origin browser write, not an absent Origin."""

    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if request.headers.get("origin") != expected:
        raise HTTPException(status_code=403, detail="disclosure_origin_required")


def _require_disclosure_csrf(request: Request, profile_id: str) -> None:
    _require_disclosure_origin(request)
    try:
        disclosure_approvals.check_csrf(
            profile_id,
            request.cookies.get(SESSION_COOKIE, ""),
            request.headers.get("x-neo-disclosure-csrf", ""),
        )
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/external-agents/disclosures/csrf")
def disclosure_csrf(request: Request) -> dict[str, str]:
    profile_id = _disclosure_profile(request)
    try:
        token = disclosure_approvals.csrf_token(profile_id, request.cookies.get(SESSION_COOKIE, ""))
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"token": token}


@router.get("/external-agents/disclosures")
def list_disclosures(request: Request, chat_id: int = Query(gt=0)) -> list[dict]:
    profile_id = _disclosure_profile(request)
    return disclosure_approvals.list_for_chat(profile_id, chat_id)


@router.post("/external-agents/disclosures/preview")
def preview_disclosure(payload: DisclosurePreviewRequest, request: Request) -> dict:
    profile_id = _disclosure_profile(request)
    _require_disclosure_csrf(request, profile_id)
    try:
        return disclosure_approvals.preview(
            profile_id=profile_id,
            chat_id=payload.chat_id,
            request_id=payload.request_id,
            engine=payload.engine,
            objective=payload.objective,
            source_message_id=payload.source_message_id,
            source_note_id=payload.source_note_id,
        )
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/external-agents/disclosures/{approval_id}/approve")
def approve_disclosure(approval_id: str, request: Request) -> dict[str, bool]:
    profile_id = _disclosure_profile(request)
    _require_disclosure_csrf(request, profile_id)
    try:
        disclosure_approvals.decide(profile_id, approval_id, approve=True)
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"approved": True}


@router.delete("/external-agents/disclosures/{approval_id}")
def revoke_disclosure(approval_id: str, request: Request) -> dict[str, bool]:
    profile_id = _disclosure_profile(request)
    _require_disclosure_csrf(request, profile_id)
    try:
        disclosure_approvals.revoke(profile_id, approval_id)
    except disclosure_approvals.DisclosureApprovalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"revoked": True}


@router.post("/external-agents/grants/{approval_id}/revoke")
def revoke_grant_alias(approval_id: str, request: Request) -> dict[str, bool]:
    """Contract route for revocation; the composer also uses DELETE above."""

    return revoke_disclosure(approval_id, request)
