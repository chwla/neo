"""Running an external coding CLI as a child process, safely and cancellably.

This is deliberately *not* ``command_sandbox``. That module exists to run a short
allowlisted command with a scrubbed environment and no network; this one runs a
long-lived, credentialed, network-enabled agent that edits files. Its policy
would reject every invocation here, and borrowing it would misrepresent what is
happening. The containment that does apply is stated in ``run_process``.

Three things make this harder than ``subprocess.run``:

* **Output must stream.** A coding run takes minutes and the user watches the
  trace; buffering until exit would show nothing and then everything.
* **Cancellation is a database flip.** Neo cancels by writing to the session row,
  not by signalling a thread, so something has to notice and kill the process.
  Reading stdout blocks, so the watcher is a side thread.
* **Children outlive parents.** These CLIs spawn shells, test runners and
  language servers. Killing the process alone orphans that tree, so the child
  gets its own process group and the whole group is signalled.
"""

from __future__ import annotations

import contextvars
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from app.services.external_agents.types import RunOutcome

_LOG = logging.getLogger(__name__)

#: Kept from the child's stderr for a failure message. Enough to hold a stack
#: trace or a usage error; small enough that a chatty process cannot exhaust
#: memory over a long run.
MAX_STDERR_BYTES = 64 * 1024

#: A single output line is bounded too. A CLI that prints a whole file on one
#: line should not be able to grow the parser's buffer without limit.
MAX_LINE_BYTES = 4 * 1024 * 1024

#: How often the watcher asks whether the session has been cancelled.
CANCEL_POLL_SECONDS = 1.0

#: How long a signalled process group gets to exit before it is killed outright.
TERM_GRACE_SECONDS = 5.0


def _kill_group(process: subprocess.Popen[str]) -> None:
    """Signal the whole process group, then insist.

    The CLIs spawn shells and test runners; terminating only the direct child
    leaves that tree running against the user's repository. ``start_new_session``
    is what makes the group addressable, so this and that flag are one mechanism
    in two places.
    """

    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            group = os.getpgid(process.pid)
            os.killpg(group, signal.SIGTERM)
        else:  # pragma: no cover - Windows
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        return

    try:
        process.wait(timeout=TERM_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover - Windows
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _drain_stderr(process: subprocess.Popen[str], sink: list[str]) -> None:
    """Collect stderr without letting it block the child or grow unbounded."""

    size = 0
    try:
        assert process.stderr is not None
        for line in process.stderr:
            if size < MAX_STDERR_BYTES:
                sink.append(line)
                size += len(line)
            # Past the cap we keep reading and discarding: stopping would fill
            # the pipe buffer and wedge the child on its next write.
    except (OSError, ValueError):
        pass


def run_process(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    on_line: Callable[[dict[str, Any] | str], None],
    is_cancelled: Callable[[], bool] | None = None,
) -> RunOutcome:
    """Run ``argv`` in ``cwd``, streaming each stdout line to ``on_line``.

    ``on_line`` receives a parsed JSON object, or the raw string when a line is
    not JSON -- a CLI is entitled to print a banner, and that is not a reason to
    fail a run.

    Containment, since this bypasses the command sandbox by design:
      * ``cwd`` must already be a validated repository root; this function never
        derives one.
      * ``env`` must already be the allowlist from ``env.build_env``; nothing is
        inherited here.
      * ``stdin`` is closed, so the child can never block waiting for input.
      * the child leads its own process group, and the group is what gets killed.
    """

    outcome = RunOutcome()
    stderr_lines: list[str] = []

    try:
        process = subprocess.Popen(  # noqa: S603 - argv assembled by an adapter, never raw input
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        outcome.error = f"could not start {argv[0]}: {exc}"
        return outcome

    stopping = threading.Event()
    state: dict[str, bool] = {"timed_out": False, "cancelled": False}

    def watch() -> None:
        """Kill the group when the clock runs out or the session is cancelled.

        Separate from the reader because reading stdout blocks: a cancelled run
        that is waiting on a silent child would otherwise not notice until the
        child spoke again, which for a long test run could be minutes.
        """

        deadline = time.monotonic() + timeout
        while not stopping.wait(CANCEL_POLL_SECONDS):
            if process.poll() is not None:
                return
            if time.monotonic() >= deadline:
                state["timed_out"] = True
                _kill_group(process)
                return
            if is_cancelled is not None:
                try:
                    if is_cancelled():
                        state["cancelled"] = True
                        _kill_group(process)
                        return
                except Exception:  # pragma: no cover - a probe must never kill the run
                    _LOG.warning("external_agent_cancel_probe_failed", exc_info=True)

    # The caller's context is copied in for the same reason ``agent_core.worker``
    # copies it into the worker thread: Neo selects the profile database through
    # a ContextVar, and a thread without it resolves the *base* database. The
    # cancel probe would then find no such session on every poll and kill a
    # perfectly healthy run within a second of starting it.
    watch_context = contextvars.copy_context()
    watcher = threading.Thread(
        target=lambda: watch_context.run(watch), name="neo-extagent-watch", daemon=True
    )
    watcher.start()
    errors = threading.Thread(
        target=_drain_stderr, args=(process, stderr_lines), name="neo-extagent-err", daemon=True
    )
    errors.start()

    try:
        assert process.stdout is not None
        for raw in process.stdout:
            line = raw.strip()
            if not line:
                continue
            if len(line) > MAX_LINE_BYTES:
                line = line[:MAX_LINE_BYTES]
            on_line(_parse(line))
    except (OSError, ValueError) as exc:
        # A broken pipe here means the child died -- usually because we killed
        # it. The exit code below decides what that means.
        _LOG.info("external_agent_stdout_ended argv0=%s", argv[0], exc_info=exc)
    finally:
        stopping.set()
        try:
            process.wait(timeout=TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                process.wait(timeout=TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - kill -9 refused
                _LOG.error("external_agent_would_not_die argv0=%s", argv[0])
        errors.join(timeout=2.0)
        watcher.join(timeout=2.0)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:  # pragma: no cover
                pass

    outcome.exit_code = process.returncode
    outcome.timed_out = state["timed_out"]
    outcome.cancelled = state["cancelled"]
    stderr_text = "".join(stderr_lines).strip()

    if state["cancelled"]:
        outcome.outcome = "failed"
        outcome.error = "cancelled"
    elif state["timed_out"]:
        outcome.outcome = "failed"
        outcome.error = f"the run exceeded its {int(timeout)}s time limit and was stopped"
    elif process.returncode == 0:
        outcome.outcome = "completed"
    else:
        outcome.outcome = "failed"
        tail = _tail(stderr_text)
        outcome.error = (
            f"{os.path.basename(argv[0])} exited with code {process.returncode}"
            + (f": {tail}" if tail else "")
        )
    return outcome


def _parse(line: str) -> dict[str, Any] | str:
    import json

    try:
        value = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    return value if isinstance(value, dict) else line


def _tail(text: str, *, lines: int = 6) -> str:
    """The last few stderr lines -- where the actual reason usually is."""

    if not text:
        return ""
    return " | ".join(text.splitlines()[-lines:])[:1000]


def iter_jsonl(lines: Iterable[str]) -> Iterable[dict[str, Any] | str]:
    """Parse an iterable of raw lines the way ``run_process`` does (for tests)."""

    for raw in lines:
        line = raw.strip()
        if line:
            yield _parse(line)


__all__ = ["MAX_STDERR_BYTES", "iter_jsonl", "run_process"]
