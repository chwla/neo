"""Signing an external CLI in, from inside Neo.

The composer used to end at a dead end. An engine that was installed but signed
out was drawn greyed out with its reason, and the reason was a command --
``claude auth login`` -- that the person reading it had no way to run from where
they were standing. This module is the missing half: Neo starts the CLI's own
sign-in, relays what it says, and takes back the one piece of input it asks for.

Three things about *how* are load-bearing.

**It runs under a pty.** Both CLIs are interactive terminal programs. Handed a
pipe they either abort or silently skip the prompt, so a login driven through
``subprocess.PIPE`` fails in a way that looks like a Neo bug. A pseudo-terminal
is what makes them behave exactly as they do in a real shell.

**The two CLIs finish differently, and neither is wrong.** ``codex login``
starts a local callback server and completes on its own once the browser comes
back. ``claude auth login`` redirects to a page that displays a code, and then
waits on stdin for it. So this exposes a code channel, used by one and ignored
by the other, rather than pretending the two flows are the same.

**Neo never handles the credentials.** It spawns the CLI, forwards a code the
user pasted, and afterwards asks ``detect`` whether the CLI now says it is
signed in. It does not read, write, parse or store a token, and the pasted code
is written to the child's stdin and kept nowhere -- not in the buffer that is
read back to the browser, and not in a log line.
"""

from __future__ import annotations

import logging
import os
import re
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.services.external_agents import detect
from app.services.external_agents import env as env_module
from app.services.external_agents.types import ExternalAgentError

_LOG = logging.getLogger(__name__)

#: The sign-in each CLI offers, as argv after the binary.
LOGIN_ARGV: dict[str, list[str]] = {
    "claude_code": ["auth", "login"],
    "codex": ["login"],
}

#: The same thing as a person would type it. Shown whenever Neo cannot drive the
#: flow itself -- on Windows, or after a failure -- so the fallback is always a
#: command someone can actually run rather than an apology.
LOGIN_COMMAND: dict[str, str] = {
    "claude_code": "claude auth login",
    "codex": "codex login",
}

#: A browser sign-in that nobody completes must not leave a process holding a pty
#: for the life of the server. Ten minutes is far longer than the flow takes and
#: short enough to be a bound.
LOGIN_TIMEOUT_SECONDS = 600

#: How much of the CLI's output is kept to show the user. It is a handful of
#: lines plus a long URL; this is generous, and it is a cap rather than a
#: guess so a CLI that decides to stream cannot grow the buffer without limit.
MAX_OUTPUT_CHARS = 16_000

#: Terminal control sequences, stripped so the text can be read back as text.
#: OSC first: it is the one that *contains* a URL (the hyperlink escape both
#: CLIs emit), and removing CSI first would leave its payload stranded.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)")
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ESC = re.compile(r"\x1b[@-Z\\-_]")

_URL = re.compile(r"https?://[^\s\x00-\x1f\"'<>\\\]]+")

#: What the visible text looks like when a CLI is waiting for a pasted code.
_CODE_PROMPT = ("paste code", "paste the code", "authorization code", "enter the code")


def _sanitize(raw: str) -> str:
    """Terminal output as plain text.

    Carriage returns become newlines rather than being dropped: a CLI redrawing
    a line uses them as separators, and collapsing them would run two distinct
    messages together into one unreadable line.
    """

    text = _OSC.sub("", raw)
    text = _CSI.sub("", text)
    text = _ESC.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find_url(text: str) -> str | None:
    """The sign-in URL, when the CLI has printed one.

    Both CLIs print exactly one link and it is the one to follow, but they also
    print it twice -- once as a hyperlink escape and once as visible text -- so
    this takes the first match after sanitising and prefers one that looks like
    an authorisation endpoint over, say, a docs link in a footer.
    """

    matches = _URL.findall(text)
    if not matches:
        return None
    for candidate in matches:
        lowered = candidate.lower()
        if "oauth" in lowered or "auth" in lowered or "login" in lowered:
            return candidate
    return matches[0]


def _disable_echo(slave_fd: int) -> None:
    """Stop the terminal echoing what Neo writes into it.

    A pty echoes input back to the reader by default, so without this the code
    a user pastes would arrive straight back in the buffer that is read out to
    the browser. Best effort: a platform whose termios refuses is not a reason
    to fail the sign-in, and ``_redact`` still covers it.
    """

    try:
        import termios

        attrs = termios.tcgetattr(slave_fd)
        attrs[3] &= ~termios.ECHO  # lflag
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except Exception as exc:  # pragma: no cover - platform dependent
        _LOG.debug("external_login_echo_off_failed", exc_info=exc)


def _redact(text: str, secrets: list[str]) -> str:
    """Remove anything the user submitted from text on its way to the browser."""

    for secret in secrets:
        if secret:
            text = text.replace(secret, "[code]")
    return text


@dataclass
class _Attempt:
    """One in-flight sign-in. At most one per executor, by construction."""

    executor: str
    process: subprocess.Popen[bytes]
    master_fd: int
    started_at: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    text: str = ""
    finished: bool = False
    exit_code: int | None = None
    error: str | None = None
    timed_out: bool = False
    cancelled: bool = False
    #: Codes the user has submitted, kept only so they can be scrubbed back out
    #: of ``text``. Belt and braces against the pty echoing what we wrote and
    #: against a CLI that draws its own input line: ECHO is turned off below,
    #: but a program in raw mode renders the characters itself and neither
    #: measure alone covers both cases.
    secrets: list[str] = field(default_factory=list)
    #: The freshly re-probed detection row, once the attempt has ended. This,
    #: not the exit code, is what decides whether the sign-in worked: a CLI that
    #: exits zero having done nothing is still signed out.
    outcome: dict[str, Any] | None = None
    #: The pump, so shutdown can wait for it rather than leaving a thread that
    #: is about to re-probe detection running into whatever comes next.
    thread: threading.Thread | None = None


_ATTEMPTS: dict[str, _Attempt] = {}
_REGISTRY_LOCK = threading.Lock()


def _kill(attempt: _Attempt) -> None:
    """Signal the whole group, then insist -- as ``runner`` does for a run.

    A login spawns a browser opener and, for Codex, a local HTTP listener that
    holds port 1455. Terminating only the direct child leaves that port bound
    and the next attempt fails with an error about the port rather than about
    the sign-in.
    """

    process = attempt.process
    if process.poll() is not None:
        return
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:  # pragma: no cover - Windows
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        return
    try:
        process.wait(timeout=5)
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


def _pump(attempt: _Attempt) -> None:
    """Read the pty until the CLI is done, then ask whether it worked.

    ``select`` rather than a blocking read, because the deadline has to be
    enforced against a flow that is, by its nature, waiting for a human.
    """

    deadline = attempt.started_at + LOGIN_TIMEOUT_SECONDS
    try:
        while True:
            if time.time() > deadline:
                with attempt.lock:
                    attempt.timed_out = True
                _kill(attempt)
                break
            try:
                ready, _, _ = select.select([attempt.master_fd], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                if attempt.process.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(attempt.master_fd, 4096)
            except OSError:
                # EIO on the master is how a pty reports "the child closed the
                # other end", which is a normal end of output, not a failure.
                break
            if not chunk:
                break
            piece = _sanitize(chunk.decode("utf-8", "replace"))
            with attempt.lock:
                attempt.text = (attempt.text + piece)[-MAX_OUTPUT_CHARS:]
    finally:
        try:
            attempt.process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            _kill(attempt)
        try:
            os.close(attempt.master_fd)
        except OSError:
            pass

        # The CLI has written its credentials by now, so the cached probe is
        # stale by definition. Re-probing here rather than on the next poll
        # means the answer is ready the moment the UI asks.
        detect.clear_cache()
        try:
            row = detect.inspect(attempt.executor, refresh=True)
        except Exception as exc:  # pragma: no cover - a probe must not strand a login
            _LOG.info("external_login_probe_failed executor=%s", attempt.executor, exc_info=exc)
            row = None

        with attempt.lock:
            attempt.exit_code = attempt.process.returncode
            attempt.outcome = row
            attempt.finished = True
            if attempt.cancelled:
                attempt.error = "Sign-in cancelled."
            elif attempt.timed_out:
                attempt.error = "Sign-in timed out before it was completed."
            elif row is not None and not row.get("available"):
                # Exit code is not the test. Report what the CLI now says about
                # itself, which is the thing that decides whether a run works.
                attempt.error = str(row.get("reason") or "the CLI still reports it is signed out")
            elif row is None:
                attempt.error = "Signed in, but Neo could not re-check the CLI."


def start(executor: str) -> dict[str, Any]:
    """Begin (or re-join) a sign-in for one executor.

    Idempotent while one is running: a second click re-attaches to the attempt
    in flight instead of starting a rival process that would race it for the
    same callback port.
    """

    from app.services import chat_prefs

    if executor not in detect.SPECS:
        raise ExternalAgentError(f"Unknown engine '{executor}'.")
    if not chat_prefs.external_agents_enabled():
        raise ExternalAgentError(
            "External engines are off for this profile. Turn them on before signing in."
        )

    with _REGISTRY_LOCK:
        existing = _ATTEMPTS.get(executor)
        # Decide inside the lock, report outside it. ``state`` takes the same
        # lock, and it is not reentrant: reading the answer in here would
        # deadlock the request thread on a second click and leave the registry
        # locked against every later one.
        rejoin = existing is not None and not existing.finished
    if rejoin:
        return state(executor)

    spec = detect.SPECS[executor]
    binary = detect.resolve_binary(executor)
    if not binary:
        raise ExternalAgentError(
            f"`{spec.program}` is not installed, so there is nothing to sign in to. "
            f"Install it first, then sign in."
        )
    if not hasattr(os, "openpty"):  # pragma: no cover - Windows
        raise ExternalAgentError(
            f"Neo cannot drive this sign-in on this platform. "
            f"Run `{LOGIN_COMMAND[executor]}` in a terminal, then re-check."
        )

    import pty

    master_fd, slave_fd = pty.openpty()
    _disable_echo(slave_fd)
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built here, never user input
            [binary, *LOGIN_ARGV[executor]],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=env_module.build_env(
                home_env=spec.home_env,
                home_dir=str(getattr(get_settings(), spec.home_setting, "") or ""),
                interactive=True,
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        os.close(master_fd)
        os.close(slave_fd)
        raise ExternalAgentError(f"Could not start {spec.name} sign-in: {exc}") from exc
    finally:
        # The parent's copy of the slave end must go, or the read side never
        # sees EOF and the pump waits forever on a process that has exited.
        try:
            os.close(slave_fd)
        except OSError:
            pass

    attempt = _Attempt(
        executor=executor, process=process, master_fd=master_fd, started_at=time.time()
    )
    attempt.thread = threading.Thread(
        target=_pump, args=(attempt,), name=f"neo-login-{executor}", daemon=True
    )
    with _REGISTRY_LOCK:
        _ATTEMPTS[executor] = attempt
    attempt.thread.start()
    _LOG.info("external_login_started executor=%s", executor)
    return state(executor)


def submit_code(executor: str, code: str) -> dict[str, Any]:
    """Hand the CLI the code the user pasted from the sign-in page.

    The code goes straight to the child's stdin. It is deliberately not appended
    to ``attempt.text`` and never logged: that buffer is read back to the
    browser, and a short-lived credential does not belong in it.
    """

    with _REGISTRY_LOCK:
        attempt = _ATTEMPTS.get(executor)
    if attempt is None or attempt.finished:
        raise ExternalAgentError("There is no sign-in waiting for a code.")
    value = (code or "").strip()
    if not value:
        raise ExternalAgentError("Enter the code from the sign-in page.")
    with attempt.lock:
        attempt.secrets.append(value)
    try:
        os.write(attempt.master_fd, (value + "\n").encode())
    except OSError as exc:
        raise ExternalAgentError(f"Could not send the code: {exc}") from exc
    return state(executor)


def cancel(executor: str) -> dict[str, Any]:
    """Stop a sign-in that is in flight."""

    with _REGISTRY_LOCK:
        attempt = _ATTEMPTS.get(executor)
    if attempt is None or attempt.finished:
        return state(executor)
    with attempt.lock:
        attempt.cancelled = True
    _kill(attempt)
    return state(executor)


def state(executor: str) -> dict[str, Any]:
    """What the interface needs to draw the sign-in, at this moment."""

    command = LOGIN_COMMAND.get(executor, "")
    with _REGISTRY_LOCK:
        attempt = _ATTEMPTS.get(executor)
    if attempt is None:
        return {
            "executor": executor,
            "running": False,
            "finished": False,
            "url": None,
            "needs_code": False,
            "output": "",
            "error": None,
            "exit_code": None,
            "command": command,
            "status": None,
        }
    with attempt.lock:
        text = _redact(attempt.text, attempt.secrets)
        finished = attempt.finished
        error = attempt.error
        exit_code = attempt.exit_code
        outcome = dict(attempt.outcome) if attempt.outcome else None
    lowered = text.lower()
    return {
        "executor": executor,
        "running": not finished,
        "finished": finished,
        "url": _find_url(text),
        # Only worth asking for while the CLI is still there to receive it.
        "needs_code": (not finished) and any(hint in lowered for hint in _CODE_PROMPT),
        "output": text.strip(),
        "error": error,
        "exit_code": exit_code,
        "command": command,
        "status": outcome,
    }


def states() -> dict[str, dict[str, Any]]:
    """Every executor's sign-in state, for one round trip instead of two."""

    return {name: state(name) for name in detect.SPECS}


def reset() -> None:
    """Drop every attempt, killing any still running. For tests and shutdown.

    The pumps are joined rather than abandoned. Each one ends by re-probing
    detection, and a thread that reaches that line after its caller has moved on
    would run a real probe against whatever the process looks like by then.
    """

    with _REGISTRY_LOCK:
        attempts = list(_ATTEMPTS.values())
        _ATTEMPTS.clear()
    for attempt in attempts:
        if not attempt.finished:
            _kill(attempt)
    for attempt in attempts:
        if attempt.thread is not None:
            attempt.thread.join(timeout=15)


__all__ = [
    "LOGIN_COMMAND",
    "LOGIN_TIMEOUT_SECONDS",
    "cancel",
    "reset",
    "start",
    "state",
    "states",
    "submit_code",
]
