from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.services.git.safety import validate_git_args

OUTPUT_LIMIT = 300 * 1024


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(cwd: Path, args: list[str], *, timeout: int = 30, check: bool = True) -> GitResult:
    validate_git_args(args)
    executable = shutil.which("git")
    if not executable:
        raise RuntimeError("Git is not installed in this runtime.")
    home = tempfile.mkdtemp(prefix="neo-git-home-")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C.UTF-8",
    }
    try:
        completed = subprocess.run(
            [executable, *args],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            shell=False,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Git operation timed out after {timeout} seconds.") from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)
    stdout_bytes = completed.stdout[:OUTPUT_LIMIT]
    stderr_bytes = completed.stderr[:OUTPUT_LIMIT]
    truncated = len(completed.stdout) > OUTPUT_LIMIT or len(completed.stderr) > OUTPUT_LIMIT
    result = GitResult(
        returncode=completed.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
        truncated=truncated,
    )
    if check and completed.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "Git operation failed."
        raise RuntimeError(detail[:2000])
    return result
