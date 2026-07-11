from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

OUTPUT_LIMIT = 64 * 1024


def run(argv: list[str], cwd: Path, timeout_ms: int) -> dict:
    started = time.monotonic()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
        "CI": "true",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
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
