from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from app.core.paths import IS_WINDOWS, is_within

OUTPUT_LIMIT = 64 * 1024

#: Variables a child process genuinely needs to start on Windows. ``SystemRoot``
#: is not a convenience: without it the CRT cannot initialise winsock and many
#: programs, Python included, fail before running. The environment stays an
#: allowlist -- these are added to it, not inherited wholesale.
_WINDOWS_PASSTHROUGH = ("SystemRoot", "windir", "SystemDrive", "PATHEXT", "TEMP", "TMP", "COMSPEC")


def _child_env(cwd: Path) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
    }
    if IS_WINDOWS:
        # The Windows spelling of HOME. Set to the workspace for the same reason
        # HOME is on POSIX: a tool that writes a cache or config file should put
        # it in the sandbox, not in the user's profile.
        env["USERPROFILE"] = str(cwd)
        for name in _WINDOWS_PASSTHROUGH:
            value = os.environ.get(name)
            if value:
                env[name] = value
    else:
        env["HOME"] = str(cwd)
    return env


def resolve_program(program: str, cwd: Path, path: str) -> str:
    """Find the executable for an already-allowlisted program name.

    Needed on Windows, where ``npm`` is ``npm.cmd`` and ``shell=False`` will not
    apply PATHEXT for you, so the allowlisted name never starts.

    The workspace check is the point of doing this by hand. ``shutil.which``
    puts the *current directory* first on Windows, and the current directory
    here is the repository the agent is working in -- so a repo carrying its own
    ``pytest.exe`` would be run in place of the real one. Resolution is confined
    to PATH, and a result that still lands inside the workspace is refused.
    """

    found = shutil.which(program, path=path)
    if not found:
        raise FileNotFoundError(
            f"'{program}' is not installed on this machine, so Neo cannot run it here."
        )
    resolved = Path(found).resolve()
    if is_within(cwd.resolve(), resolved):
        raise PermissionError(
            f"'{program}' resolved to a file inside the workspace; refusing to run it."
        )
    return str(resolved)


def run(argv: list[str], cwd: Path, timeout_ms: int) -> dict:
    started = time.monotonic()
    env = _child_env(cwd)
    try:
        argv = [resolve_program(argv[0], cwd, env["PATH"]), *argv[1:]]
    except (FileNotFoundError, PermissionError) as exc:
        return {
            "status": "completed",
            "exit_code": 127,
            "stdout_text": "",
            "stderr_text": str(exc),
            "output_truncated": False,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
            shell=False,
            check=False,
        )
        stdout, stderr = result.stdout or "", result.stderr or ""
        timed_out = False
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        timed_out = True
        exit_code = None
    truncated = len(stdout.encode()) > OUTPUT_LIMIT or len(stderr.encode()) > OUTPUT_LIMIT
    return {
        "status": "timed_out" if timed_out else "completed",
        "exit_code": exit_code,
        "stdout_text": stdout[:OUTPUT_LIMIT],
        "stderr_text": stderr[:OUTPUT_LIMIT],
        "output_truncated": truncated,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
