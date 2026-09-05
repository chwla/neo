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

SPECS: dict[str, ExecutorSpec] = {CLAUDE_CODE.id: CLAUDE_CODE, CODEX.id: CODEX}

_CACHE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def spec(executor: str) -> ExecutorSpec | None:
    return SPECS.get(executor)


def resolve_binary(executor: str) -> str | None:
    """The path to run, preferring an explicitly configured one over PATH."""

    executor_spec = SPECS.get(executor)
    if executor_spec is None:
        return None
    configured = str(getattr(get_settings(), executor_spec.bin_setting, "") or "").strip()
    if configured:
        # An explicit path that does not exist is a configuration error worth
        # reporting as such, rather than silently falling back to PATH and
        # running a different binary than the one that was asked for.
        return configured if shutil.which(configured) or _is_executable(configured) else None
    return shutil.which(executor_spec.program)


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


def _claude_auth(binary: str, executor_spec: ExecutorSpec) -> tuple[str | None, str | None]:
    """(auth, reason). ``claude auth status --json`` is machine-readable."""

    result = _run([binary, "auth", "status", "--json"], executor_spec)
    if result is None:
        return None, "could not run `claude auth status`"
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError:
        # The command ran but said something we do not understand. That is not
        # evidence of being signed in.
        return "unknown", None
    if not data.get("loggedIn"):
        return None, "not signed in -- run `claude auth login`"
    method = str(data.get("authMethod") or "")
    if method == "claude.ai":
        return "subscription", None
    if method:
        return "api_key" if "key" in method.lower() else method, None
    return "unknown", None


def _codex_auth(binary: str, executor_spec: ExecutorSpec) -> tuple[str | None, str | None]:
    """(auth, reason). ``codex login status`` prints prose, so parse narrowly.

    Only two phrasings are recognised. Anything else is reported as ``unknown``
    rather than being coerced into a category -- a wrong guess here is worse than
    an honest shrug, because it decides whether the composer offers the executor.
    """

    result = _run([binary, "login", "status"], executor_spec)
    if result is None:
        return None, "could not run `codex login status`"
    text = f"{result.stdout} {result.stderr}".strip().lower()
    if result.returncode != 0 or "not logged in" in text or "no credentials" in text:
        return None, "not signed in -- run `codex login`"
    if "chatgpt" in text:
        return "subscription", None
    if "api key" in text:
        return "api_key", None
    return "unknown", None


_AUTH_PROBES = {"claude_code": _claude_auth, "codex": _codex_auth}


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
    }

    binary = resolve_binary(executor)
    if not binary:
        configured = str(getattr(get_settings(), executor_spec.bin_setting, "") or "").strip()
        row["reason"] = (
            f"configured path '{configured}' is not executable"
            if configured
            else f"`{executor_spec.program}` not found on PATH"
        )
        return row

    version = _run([binary, "--version"], executor_spec)
    if version is None or version.returncode != 0:
        row["reason"] = f"`{executor_spec.program} --version` failed"
        return row
    row["version"] = (version.stdout or version.stderr or "").strip().splitlines()[0][:120]

    auth, reason = _AUTH_PROBES[executor](binary, executor_spec)
    row["auth"] = auth
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
    "CLAUDE_CODE",
    "CODEX",
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
