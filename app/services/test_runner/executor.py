from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import remove_tree
from app.core.processes import ProcessTree
from app.services import isolation
from app.services.isolation import launch

_LOG = logging.getLogger(__name__)

STDOUT_LIMIT = 200 * 1024
STDERR_LIMIT = 200 * 1024
COMBINED_LIMIT = 300 * 1024


class _RunUnsandboxed(Exception):
    """Internal: take the approved unsandboxed branch below."""


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    exit_code: int | None
    stdout_text: str
    stderr_text: str
    combined_output: str
    duration_ms: int
    error: str | None
    metadata: dict[str, bool]


def _read_limited(stream, limit: int) -> tuple[str, bool]:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    data = stream.read(limit)
    return data.decode("utf-8", errors="replace"), size > limit


def _resolved_argv(command: list[str]) -> list[str]:
    executable = sys.executable if command[0] == "python" else shutil.which(command[0])
    if not executable:
        raise RuntimeError(
            f"Allowlisted executable '{command[0]}' is not installed in this runtime."
        )
    return [executable, *command[1:]]


def _active_profile_id() -> str:
    from app.core.config import active_profile_storage_dir

    storage = active_profile_storage_dir.get()
    return Path(storage).name if storage else ""


def execute(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    *,
    subject: str = "",
    approval: launch.HumanApproval | None = None,
) -> ExecutionResult:
    started = time.monotonic()
    home = tempfile.mkdtemp(prefix="neo-test-home-")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    timed_out = False
    error = None
    exit_code = None
    unsandboxed = False
    try:
        argv = _resolved_argv(command)
        # A test suite is the repository's own code, and on this path the repository
        # is one an agent has just edited -- a `conftest.py` or an npm script it
        # wrote runs here. So the launch goes through ``isolation.launch``: isolated
        # exactly like the agent's own, or unsandboxed only because the person who
        # started this run approved it knowing that, or not at all.
        stdout_truncated = stderr_truncated = False
        mode = launch.authorize("test_runner", subject=subject, approval=approval)
        unsandboxed = mode == "unsandboxed"
        try:
            if unsandboxed:
                raise _RunUnsandboxed
            outcome = isolation.run(
                argv,
                cwd=str(cwd),
                env=environment,
                purpose="test_runner",
                profile_id=_active_profile_id(),
                workspace=str(cwd),
                timeout=float(timeout_seconds),
            )
            stdout_text = outcome.stdout[:STDOUT_LIMIT]
            stderr_text = outcome.stderr[:STDERR_LIMIT]
            stdout_truncated = len(outcome.stdout) > STDOUT_LIMIT
            stderr_truncated = len(outcome.stderr) > STDERR_LIMIT
            timed_out = outcome.timed_out
            exit_code = outcome.exit_code
            if outcome.quarantined:
                error = outcome.quarantine_reason
        except (isolation.IsolationRefused, isolation.IsolationUnavailable) as exc:
            # Includes a sandbox that failed part way into the launch: that is a
            # refusal, never a cue to run the suite without one.
            stdout_text = stderr_text = ""
            error = str(exc)
        except _RunUnsandboxed:
            # No sandbox here, and the person who started this run approved running
            # it without one after being told what that exposes.
            _LOG.warning("test_runner_unisolated platform=%s subject=%s", sys.platform, subject)
            with (
                tempfile.TemporaryFile() as stdout_file,
                tempfile.TemporaryFile() as stderr_file,
            ):
                tree = ProcessTree(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                )
                try:
                    exit_code = tree.process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    tree.kill()
                    tree.process.wait()
                finally:
                    tree.close()
                stdout_text, stdout_truncated = _read_limited(stdout_file, STDOUT_LIMIT)
                stderr_text, stderr_truncated = _read_limited(stderr_file, STDERR_LIMIT)
    except Exception as exc:
        stdout_text, stderr_text = "", ""
        stdout_truncated = stderr_truncated = False
        error = str(exc)
    finally:
        remove_tree(home, ignore_errors=True)

    combined = stdout_text + (("\n" if stdout_text and stderr_text else "") + stderr_text)
    combined_truncated = len(combined.encode("utf-8")) > COMBINED_LIMIT
    if combined_truncated:
        combined = combined.encode("utf-8")[:COMBINED_LIMIT].decode("utf-8", errors="ignore")
    status = (
        "error" if error else "timed_out" if timed_out else "passed" if exit_code == 0 else "failed"
    )
    return ExecutionResult(
        status=status,
        exit_code=exit_code,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        combined_output=combined,
        duration_ms=round((time.monotonic() - started) * 1000),
        error=error,
        metadata={
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "combined_truncated": combined_truncated,
            "unsandboxed": unsandboxed,
        },
    )
