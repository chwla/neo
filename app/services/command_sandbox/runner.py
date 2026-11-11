from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

from app.core.child_env import ProgramRefused, child_environment
from app.core.child_env import resolve_program as _resolve_program
from app.core.processes import run_bounded
from app.services import isolation
from app.services.git import executor as git_executor
from app.services.isolation import launch

_LOG = logging.getLogger(__name__)

OUTPUT_LIMIT = 64 * 1024

def _child_env(cwd: Path) -> dict[str, str]:
    return child_environment(cwd)


def resolve_program(program: str, cwd: Path, path: str) -> list[str]:
    """The argv prefix for an already-allowlisted program name.

    Shared with the test runner (``core.child_env``): resolution is confined to
    PATH and never the workspace, and on Windows no ``.cmd``/``.bat`` shim is ever
    started -- ``npm`` runs as ``node`` plus its script -- because cmd.exe would
    re-parse the arguments the allowlist had already passed.
    """

    return _resolve_program(program, cwd, path)


def _active_profile_id() -> str:
    from app.core.config import active_profile_storage_dir

    storage = active_profile_storage_dir.get()
    return Path(storage).name if storage else ""


def git_invocation(
    argv: list[str], cwd: Path, workspace: Path, env: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """An allowlisted ``git`` command, hardened the way Neo's git service runs git.

    The policy admits git's inspection subcommands with any arguments, and they
    look read-only. They are not, unhardened: git reads the repository's own
    configuration and attributes, and several settings there name a program to
    run (see ``git.executor``). With ``HOME`` pointed at the workspace, a
    ``.gitconfig`` committed to the repository would also be read as the user's.
    ``argv[0]`` is the already resolved executable.
    """

    environment = {
        **env,
        **git_executor.hardened_environment(str(cwd), workspace.parent),
        # As in the isolated git service: global configuration is discarded above,
        # so the workspace standing in for these only supplies harmless defaults.
        "XDG_CONFIG_HOME": str(cwd),
    }
    hardened = git_executor.harden_args(argv[1:], argv[0], cwd, environment)
    return [argv[0], *hardened], environment


def _failed(started: float, reason: str, stdout: str = "") -> dict:
    return {
        "status": "failed",
        "exit_code": None,
        "stdout_text": stdout[:OUTPUT_LIMIT],
        "stderr_text": reason,
        "output_truncated": False,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def run(
    argv: list[str],
    cwd: Path,
    timeout_ms: int,
    *,
    workspace: Path | None = None,
    subject: str = "",
    approval: launch.HumanApproval | None = None,
) -> dict:
    started = time.monotonic()
    env = _child_env(cwd)
    is_git = argv[0] == "git"
    try:
        argv = [*resolve_program(argv[0], cwd, env["PATH"]), *argv[1:]]
    except (FileNotFoundError, ProgramRefused, PermissionError) as exc:
        return {
            "status": "completed",
            "exit_code": 127,
            "stdout_text": "",
            "stderr_text": str(exc),
            "output_truncated": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    # The allowlist decides *which* programs may run; it says nothing about what
    # they may then read, and several allowlisted ones -- `cat`, `grep`, `find` --
    # read anything they are pointed at. `pytest` and `npm run` are worse: they run
    # the repository's own code, which on this path an agent may just have written.
    # Measured, before isolation: `cat <neo data dir>/profiles/registry.db` worked.
    # So the launch goes through the one rule in ``isolation.launch``: sandboxed, or
    # unsandboxed only because a person approved exactly this run, or not at all.
    try:
        mode = launch.authorize("command_sandbox", subject=subject, approval=approval)
    except launch.UnsandboxedLaunchRefused as exc:
        return _failed(started, str(exc))
    try:
        if is_git:
            # Inside the try: hardening runs git itself, and a refusal there is
            # reported like a refusal of the command.
            argv, env = git_invocation(argv, cwd, workspace or cwd, env)
    except (isolation.IsolationRefused, isolation.IsolationUnavailable) as exc:
        return _failed(started, str(exc))
    if mode == "isolated":
        try:
            result = isolation.run(
                argv,
                cwd=str(cwd),
                env=env,
                purpose="command_sandbox",
                profile_id=_active_profile_id(),
                workspace=str(cwd),
                timeout=timeout_ms / 1000,
            )
        except (isolation.IsolationRefused, isolation.IsolationUnavailable) as exc:
            # A sandbox that fails part way into a launch is a refusal, never a cue
            # to run the command without it.
            return _failed(started, str(exc))
        stdout, stderr = result.stdout or "", result.stderr or ""
        timed_out = result.timed_out
        exit_code = result.exit_code
        if result.quarantined:
            # Something the command started is still running. Reported as a failure
            # rather than a result, because its output cannot be trusted to be
            # complete and its files may still be changing.
            return _failed(started, result.quarantine_reason or "", stdout)
    else:
        _LOG.warning("command_sandbox_unisolated platform=%s subject=%s", sys.platform, subject)
        stdout, stderr, exit_code, timed_out = run_bounded(
            argv, cwd=cwd, env=env, timeout=timeout_ms / 1000
        )
    truncated = len(stdout.encode()) > OUTPUT_LIMIT or len(stderr.encode()) > OUTPUT_LIMIT
    return {
        "status": "timed_out" if timed_out else "completed",
        "exit_code": exit_code,
        "stdout_text": stdout[:OUTPUT_LIMIT],
        "stderr_text": stderr[:OUTPUT_LIMIT],
        "output_truncated": truncated,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
