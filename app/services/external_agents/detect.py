"""Is this executor actually usable, and can we say so honestly?

Three things have to be true before Neo offers an external executor, and they
fail in different ways that the user fixes differently:

* the feature is enabled at all,
* the binary exists,
* the CLI is signed in.

So detection reports a *reason*, not a boolean. "Claude Code unavailable" sends
someone to the wrong place; "not signed in -- run `claude auth login`" does not.

On authentication the rule is honesty over optimism. We report ``subscription``
only when the CLI says so in a form we can actually read. Where a CLI does not
make its auth state machine-readable we report ``unknown`` rather than guessing,
because a wrong "you're signed in" turns into a failed run several minutes later
with a worse error than the one we could have given up front.

Results are cached per process: a ``--version`` and an auth probe are two process
spawns, and the composer asks on every load.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.external_agents import env as env_module
from app.services.external_agents.types import ExecutorCapabilities, ExecutorSpec

_LOG = logging.getLogger(__name__)

#: Probes are quick. A CLI that cannot answer `--version` in this long is not
#: one we want to hand a coding task to.
PROBE_TIMEOUT_SECONDS = 20

#: Every value below is a measured fact, with the evidence recorded in
#: ``docs/external-agents/cli-surface.md``. Nothing here is inferred from what a
#: CLI "probably" supports.
CLAUDE_CODE = ExecutorSpec(
    id="claude_code",
    name="Claude Code",
    bin_setting="claude_code_bin",
    program="claude",
    home_setting="claude_config_dir",
    home_env="CLAUDE_CONFIG_DIR",
    session_id_key="session_id",
    capabilities=ExecutorCapabilities(
        # `--resume <uuid>`, and `--session-id` lets Neo assign the id up front.
        resume=True,
        # `--permission-mode plan`: verified to leave the repository untouched.
        # Repository protection, not filesystem isolation -- it still writes its
        # own plan files under ~/.claude/plans.
        plan_mode=True,
        # `--disallowedTools`, with per-tool scoping.
        tool_denylist=True,
        # No `--permission-prompt-tool` in 2.1.258. Neo cannot gate a tool call.
        per_tool_approval=False,
        # `result.total_cost_usd`, verified against real output.
        cost_reporting=True,
        # `result.usage.{input,output}_tokens`.
        token_reporting=True,
    ),
)

CODEX = ExecutorSpec(
    id="codex",
    name="Codex",
    bin_setting="codex_bin",
    program="codex",
    home_setting="codex_home",
    home_env="CODEX_HOME",
    # Codex mints a thread id and reports it on `thread.started`.
    session_id_key="thread_id",
    capabilities=ExecutorCapabilities(
        # `codex exec resume <thread_id>`.
        resume=True,
        # `-s read-only` on a fresh run; `-c sandbox_mode="read-only"` on resume,
        # because `codex exec resume` rejects `-s`.
        plan_mode=True,
        # `codex exec` has no per-tool allow/deny flag of any kind. Claiming
        # otherwise would assert an enforcement that does not exist.
        tool_denylist=False,
        # `-a/--ask-for-approval` is rejected by `codex exec` (exit 2) -- it is
        # a flag on the interactive command only.
        per_tool_approval=False,
        # `turn.completed` carries token counts and no cost field. Neo shows no
        # cost for Codex rather than estimating one.
        cost_reporting=False,
        # `turn.completed.usage.{input,output}_tokens`.
        token_reporting=True,
    ),
)

#: Where these two install themselves. Both vendors' installers write to
#: ``~/.local/bin``, which many login shells do not search -- so without this a
#: correctly-followed install reads back as "not found on PATH".
_LOCAL_BIN = ("~/.local/bin",)

CURSOR = ExecutorSpec(
    id="cursor",
    name="Cursor",
    bin_setting="cursor_bin",
    program="cursor-agent",
    # No configuration-directory variable is documented, and Neo does not invent
    # one: an unset home means "let the CLI find its own credentials", which is
    # the correct default for every engine here.
    home_setting="",
    home_env="",
    extra_bin_dirs=_LOCAL_BIN,
    # Cursor mints the chat id and reports it on the `system`/`init` line.
    session_id_key="session_id",
    capabilities=ExecutorCapabilities(
        # `--resume <chatId>`, and `--continue` for the previous one.
        resume=True,
        # **No read-only mode exists.** Permissions are configuration-file only
        # (`~/.cursor/cli-config.json`), so there is no flag that stops a run
        # touching the repository. Neo refuses a plan-mode run here rather than
        # quietly executing it as a normal one.
        plan_mode=False,
        # No `--disallowedTools` equivalent; deny rules live in that same file.
        tool_denylist=False,
        per_tool_approval=False,
        cost_reporting=False,
        # Undocumented in the result event, and undocumented means false here.
        token_reporting=False,
    ),
)

ANTIGRAVITY = ExecutorSpec(
    id="antigravity",
    name="Antigravity",
    bin_setting="antigravity_bin",
    program="agy",
    home_setting="",
    home_env="",
    extra_bin_dirs=_LOCAL_BIN,
    session_id_key="conversation_id",
    capabilities=ExecutorCapabilities(
        # `--conversation <id>`, and `--continue` for the most recent.
        resume=True,
        # `--sandbox` restricts the terminal, not the repository, so it is not
        # the repository protection Neo's plan mode promises.
        plan_mode=False,
        tool_denylist=False,
        # Headless soft-denies a tool it cannot get approval for; it does not
        # hand the decision back to Neo.
        per_tool_approval=False,
        cost_reporting=False,
        # A `usage` object with real token counts is documented on both the
        # result envelope and each step.
        token_reporting=True,
    ),
)

SPECS: dict[str, ExecutorSpec] = {
    CLAUDE_CODE.id: CLAUDE_CODE,
    CODEX.id: CODEX,
    CURSOR.id: CURSOR,
    ANTIGRAVITY.id: ANTIGRAVITY,
}

_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def spec(executor: str) -> ExecutorSpec | None:
    return SPECS.get(executor)


def resolve_binary(executor: str) -> str | None:
    """The path to run: an explicitly configured one, else PATH, else the installer's.

    The order is the point. A configured path wins outright and never falls
    through -- see below. PATH comes next, so a user who has their own build of a
    CLI earlier in PATH keeps getting it. Only then does ``extra_bin_dirs`` get a
    look, which is what makes a freshly-installed CLI work without anyone having
    to discover that their shell does not search where its installer wrote it.
    """

    executor_spec = SPECS.get(executor)
    if executor_spec is None:
        return None
    configured = str(getattr(get_settings(), executor_spec.bin_setting, "") or "").strip()
    if configured:
        # An explicit path that does not exist is a configuration error worth
        # reporting as such, rather than silently falling back to PATH and
        # running a different binary than the one that was asked for.
        return configured if shutil.which(configured) or _is_executable(configured) else None
    found = shutil.which(executor_spec.program)
    if found:
        return found
    for directory in executor_spec.extra_bin_dirs:
        candidate = Path(directory).expanduser() / executor_spec.program
        if _is_executable(str(candidate)):
            return str(candidate)
    return None


def _is_executable(path: str) -> bool:
    import os

    return os.path.isfile(path) and os.access(path, os.X_OK)


def _run(argv: list[str], executor_spec: ExecutorSpec) -> subprocess.CompletedProcess[str] | None:
    """Run a probe under the same environment discipline as a real run."""

    try:
        return subprocess.run(  # noqa: S603 - argv is built here, never user input
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            env=env_module.build_env(
                home_env=executor_spec.home_env,
                home_dir=str(getattr(get_settings(), executor_spec.home_setting, "") or ""),
            ),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _LOG.info("external_agent_probe_failed executor=%s", executor_spec.id, exc_info=exc)
        return None


#: What the account probe can report beyond a yes/no, kept on the row so a usage
#: panel can name the account a limit belongs to. Present as ``None`` on every row
#: whether or not the CLI answers, so the shape does not depend on the engine.
ACCOUNT_FIELDS = ("auth_method", "email", "organization", "plan")

_NO_ACCOUNT: dict[str, str | None] = dict.fromkeys(ACCOUNT_FIELDS)


def _claude_auth(
    binary: str, executor_spec: ExecutorSpec
) -> tuple[str | None, str | None, dict[str, str | None]]:
    """(auth, reason, account). ``claude auth status --json`` is machine-readable.

    The account fields come free: this probe already parses the payload they are in,
    so naming them costs no extra spawn. Read rather than inferred, and absent when
    the CLI omits them.
    """

    result = _run([binary, "auth", "status", "--json"], executor_spec)
    if result is None:
        return None, "could not run `claude auth status`", dict(_NO_ACCOUNT)
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        # The command ran but said something we do not understand. That is not
        # evidence of being signed in.
        return "unknown", None, dict(_NO_ACCOUNT)
    if not data.get("loggedIn"):
        return None, "not signed in -- run `claude auth login`", dict(_NO_ACCOUNT)
    method = str(data.get("authMethod") or "")
    account = {
        # Its own vocabulary, softened only where it is a slug rather than a name.
        "auth_method": "Claude AI" if method == "claude.ai" else (method or None),
        "email": _text(data.get("email")),
        "organization": _text(data.get("orgName")),
        "plan": _text(data.get("subscriptionType")),
    }
    if method == "claude.ai":
        return "subscription", None, account
    if method:
        return ("api_key" if "key" in method.lower() else method), None, account
    return "unknown", None, account


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _codex_auth(
    binary: str, executor_spec: ExecutorSpec
) -> tuple[str | None, str | None, dict[str, str | None]]:
    """(auth, reason, account). ``codex login status`` prints prose, so parse narrowly.

    Only two phrasings are recognised. Anything else is reported as ``unknown``
    rather than being coerced into a category -- a wrong guess here is worse than
    an honest shrug, because it decides whether the composer offers the executor.
    """

    result = _run([binary, "login", "status"], executor_spec)
    if result is None:
        return None, "could not run `codex login status`", dict(_NO_ACCOUNT)
    text = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or "not logged in" in text or "no credentials" in text:
        return None, "not signed in -- run `codex login`", dict(_NO_ACCOUNT)
    # The method is the only account fact in that sentence. Codex names no email, no
    # organization and no plan anywhere Neo can read without its credentials, so the
    # rest stays empty rather than being filled in from somewhere it does not belong.
    if "chatgpt" in text:
        return "subscription", None, {**_NO_ACCOUNT, "auth_method": "ChatGPT"}
    if "api key" in text:
        return "api_key", None, {**_NO_ACCOUNT, "auth_method": "API key"}
    return "unknown", None, dict(_NO_ACCOUNT)


def _cursor_auth(
    binary: str, executor_spec: ExecutorSpec
) -> tuple[str | None, str | None, dict[str, str | None]]:
    """(auth, reason, account). ``cursor-agent status --format json``.

    **Written against documentation, not a recorded run** -- see the Cursor
    section of ``docs/external-agents/cli-surface.md``. The command and its
    ``--format json`` are documented; the field *names* inside are not, and this
    package does not get to invent them. So the exit code carries the yes/no,
    which is the part that decides whether the engine is offered, and the shape
    inside is read for a few plausible keys and otherwise reported as ``unknown``.
    Re-check this against a real install before trusting anything more from it.
    """

    result = _run([binary, "status", "--format", "json"], executor_spec)
    if result is None:
        return None, "could not run `cursor-agent status`", dict(_NO_ACCOUNT)
    text = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or "not logged in" in text or "logged out" in text:
        return None, "not signed in -- run `cursor-agent login`", dict(_NO_ACCOUNT)
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        # It ran and exited zero, which is evidence of being signed in, but not
        # of anything more specific.
        return "unknown", None, dict(_NO_ACCOUNT)
    if not isinstance(data, dict):
        return "unknown", None, dict(_NO_ACCOUNT)
    account = {**_NO_ACCOUNT, "email": _text(data.get("email"))}
    return "unknown", None, account


def _antigravity_auth(
    binary: str, executor_spec: ExecutorSpec
) -> tuple[str | None, str | None, dict[str, str | None]]:
    """(auth, reason, account). Antigravity documents no auth-status command.

    Nothing in ``agy`` prints login state non-interactively, so the probe is
    indirect: ``agy models`` needs an authenticated account, and a headless run
    that needs a prompt is documented to exit with an actionable error. A
    non-zero exit is therefore read as signed out, and a clean one as signed in
    without claiming to know how.

    That is weaker evidence than the other three probes have, and it is the least
    verified thing in this file -- it must be checked against a real install
    before it is trusted, because a false "signed in" turns into a failed run
    several minutes later with a worse error than the one given here.
    """

    result = _run([binary, "models"], executor_spec)
    if result is None:
        return None, "could not run `agy models`", dict(_NO_ACCOUNT)
    text = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or "sign in" in text or "not authenticated" in text:
        return None, "not signed in -- run `agy` once and sign in", dict(_NO_ACCOUNT)
    return "unknown", None, dict(_NO_ACCOUNT)


_AUTH_PROBES = {
    "claude_code": _claude_auth,
    "codex": _codex_auth,
    "cursor": _cursor_auth,
    "antigravity": _antigravity_auth,
}


def _probe(executor: str) -> dict[str, Any]:
    executor_spec = SPECS[executor]
    row: dict[str, Any] = {
        "id": executor_spec.id,
        "name": executor_spec.name,
        "available": False,
        "version": None,
        "reason": None,
        "auth": None,
        # The capability record, whole and typed, rather than a handful of
        # ad-hoc booleans the frontend has to keep in step by hand.
        "capabilities": executor_spec.capabilities.model_dump(),
        **_NO_ACCOUNT,
    }

    binary = resolve_binary(executor)
    if not binary:
        configured = str(getattr(get_settings(), executor_spec.bin_setting, "") or "").strip()
        if configured:
            row["reason"] = f"configured path '{configured}' is not executable"
        else:
            # Name everywhere that was actually looked, so "not found" is a fact
            # about the search rather than an invitation to re-read the install
            # instructions that were already followed correctly.
            searched = ", ".join(("PATH", *executor_spec.extra_bin_dirs))
            row["reason"] = f"`{executor_spec.program}` not found in {searched}"
        return row

    version = _run([binary, "--version"], executor_spec)
    if version is None or version.returncode != 0:
        row["reason"] = f"`{executor_spec.program} --version` failed"
        return row
    row["version"] = (version.stdout or version.stderr or "").strip().splitlines()[0][:120]

    auth, reason, account = _AUTH_PROBES[executor](binary, executor_spec)
    row["auth"] = auth
    row.update(account)
    if auth is None:
        row["reason"] = reason or "not signed in"
        return row

    row["available"] = True
    return row


#: What an executor is called before it has been probed at all. Held apart from
#: ``_probe`` because the feature being off is a fact about the *profile*, not
#: about the machine, and the two must not be cached together -- see ``status``.
DISABLED_REASON = "external engines are off for this profile -- turn them on to use one"


def _resting_row(executor_spec: ExecutorSpec, reason: str) -> dict[str, Any]:
    return {
        "id": executor_spec.id,
        "name": executor_spec.name,
        "available": False,
        "version": None,
        "reason": reason,
        "auth": None,
        "capabilities": executor_spec.capabilities.model_dump(),
        **_NO_ACCOUNT,
    }


def inspect(executor: str, *, refresh: bool = False) -> dict[str, Any]:
    """The machine facts about one CLI: installed, which version, signed in.

    Cached, and deliberately *ungated*. Whether this profile has opted into
    external engines is a separate question, answered by :func:`status`; setup
    has to be able to say "installed and signed in, just not switched on yet",
    which a gated probe cannot express.
    """

    with _LOCK:
        if not refresh and executor in _CACHE:
            return dict(_CACHE[executor])
    row = _probe(executor)
    with _LOCK:
        _CACHE[executor] = row
    return dict(row)


def status(executor: str, *, refresh: bool = False) -> dict[str, Any]:
    """Cached availability for one executor, as the rest of Neo should read it.

    The profile gate is applied here rather than inside the cached probe. Two
    reasons, and both were bugs waiting to happen: a probe cached while one
    profile had the feature on would otherwise be handed to a profile that never
    opted in, and a profile that turns the feature on would keep reading a
    cached "it is off" long after it stopped being true.

    Nothing is spawned while the feature is off -- the early return happens
    before the probe, exactly as before.
    """

    if executor not in SPECS:
        return {"id": executor, "name": executor, "available": False, "reason": "unknown executor"}
    from app.services import chat_prefs

    if not chat_prefs.external_agents_enabled():
        return _resting_row(SPECS[executor], DISABLED_REASON)
    return inspect(executor, refresh=refresh)


def statuses(*, refresh: bool = False) -> list[dict[str, Any]]:
    return [status(name, refresh=refresh) for name in SPECS]


def inspections(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Machine facts for every executor, for the setup surface."""

    return [inspect(name, refresh=refresh) for name in SPECS]


def require_available(executor: str) -> dict[str, Any]:
    """Availability, or an error naming what to fix.

    Callers use this instead of silently running Neo's own loop when an external
    executor is missing: a turn the user asked Claude Code to run must not come
    back quietly answered by something else.
    """

    from app.services.external_agents.types import ExternalAgentError

    row = status(executor)
    if not row.get("available"):
        raise ExternalAgentError(
            f"{row.get('name', executor)} is unavailable: {row.get('reason') or 'unknown reason'}"
        )
    return row


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = [
    "ACCOUNT_FIELDS",
    "ANTIGRAVITY",
    "CLAUDE_CODE",
    "CODEX",
    "CURSOR",
    "DISABLED_REASON",
    "SPECS",
    "clear_cache",
    "inspect",
    "inspections",
    "require_available",
    "resolve_binary",
    "spec",
    "status",
    "statuses",
]
