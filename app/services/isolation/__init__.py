"""Running an untrusted process so that it cannot reach Neo's data.

This is the only way Neo starts anything that will touch a user's workspace: the
external coding CLIs, and Neo's own workspace tooling (git, the command sandbox, the
test runner, language servers). A caller names the workspace and what the run owns;
everything else on the machine is denied.

What one call does, in order, and why the order matters:

1. **Refuse an overlapping workspace.** Checked first, because once a run has
   started inside Neo's own storage no later rule helps.
2. **Build the policy** from the single protected-roots list.
3. **Create the run's markers** and prove the membership oracle works here. A broken
   oracle must never reach the kill path.
4. **Attest** -- run the policy against fresh sentinels and confirm it holds.
5. **Launch**, with the prompt on stdin.
6. **Clean up every descendant**, confirm none survives, and only then report. A run
   whose cleanup failed is returned quarantined, so the caller does not release the
   workspace or offer an undo while something may still be writing to it.

On a platform with no validated backend, every call raises
:class:`IsolationUnavailable`. There is no unsandboxed fallback: the contract's
fail-closed rule is the whole reason this module can be trusted at all.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.services.isolation import environment, procs
from app.services.isolation.attest import AttestationFailed
from app.services.isolation.attest import attest as run_attestation
from app.services.isolation.network import private_endpoint_refusal, sensitive_ports
from app.services.isolation.policy import RunPolicy, build_profile
from app.services.isolation.roots import overlaps_protected, roots, thread_state_refusal

_LOG = logging.getLogger(__name__)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"


class IsolationUnavailable(RuntimeError):
    """This machine cannot isolate a run, so Neo will not start one."""


class IsolationRefused(RuntimeError):
    """This particular run was refused: an overlap, or a failed self-check."""


@dataclass
class RunResult:
    exit_code: int | None = None
    stdout: str = ""
    stdout_bytes: bytes = b""
    output_limit_exceeded: bool = False
    stderr: str = ""
    timed_out: bool = False
    cancelled: bool = False
    quarantined: bool = False
    quarantine_reason: str = ""
    survivors: tuple = ()
    killed: tuple = ()
    lines: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.quarantined


class StreamProcess:
    """A long-running isolated process whose descendants are cleaned on exit."""

    def __init__(
        self,
        process: subprocess.Popen,
        markers: procs.RunMarkers,
        temp_dir: Path,
        lease: procs.RunLease,
    ) -> None:
        self._process = process
        self._markers = markers
        self._temp_dir = temp_dir
        self._lease = lease
        self._cleaned = False
        self._monitor_stop = threading.Event()
        self._monitor_error: str | None = None
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._monitor = threading.Thread(
            target=self._watch_members, name="neo-iso-stream-members", daemon=True
        )
        self._monitor.start()

    def _watch_members(self) -> None:
        while not self._monitor_stop.wait(1.0):
            if self._process.poll() is not None:
                return
            try:
                self._lease.update_members()
            except (OSError, procs.SupervisionUnavailable) as exc:
                self._monitor_error = str(exc)
                try:
                    procs.terminate_all(self._markers)
                except procs.SupervisionUnavailable:
                    pass
                return

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._monitor_stop.set()
        self._monitor.join(timeout=25)
        result = procs.terminate_all(self._markers)
        if not result.clean:
            raise IsolationRefused(
                f"{len(result.survivors)} streaming-process descendant(s) survived cleanup"
            )
        if self._monitor.is_alive():
            raise IsolationRefused("streaming-process membership monitor did not stop")
        if self._monitor_error is not None:
            raise IsolationRefused(
                f"streaming-process membership was not persisted: {self._monitor_error}"
            )
        self._lease.finish()
        self._markers.remove()
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._cleaned = True

    def poll(self) -> int | None:
        code = self._process.poll()
        if code is not None:
            self._cleanup()
        return code

    def wait(self, timeout: float | None = None) -> int:
        code = self._process.wait(timeout=timeout)
        self._cleanup()
        return code

    def kill(self) -> None:
        self._cleanup()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise IsolationRefused("streaming process did not exit after cleanup") from exc


def start_stream(
    argv: list[str], *, cwd: str, env: dict[str, str], profile_id: str, workspace: str
) -> StreamProcess:
    """Start an interactive tool under the attested policy and keep its pipes open."""

    require_available()
    # Language servers load workspace configuration and plugins, so what they run
    # is agent-influenced like any other run.
    exposure = environment.refusal("stream")
    if exposure is not None:
        raise IsolationUnavailable(exposure)
    recover_interrupted_runs()
    known = roots()
    workspace_path = Path(workspace).expanduser().resolve()
    refusal = overlaps_protected(workspace_path, known, profile_id=profile_id)
    if refusal is not None:
        raise IsolationRefused(f"This folder cannot be used by a streaming tool: {refusal}")
    conflict = procs.active_lease_conflict(known.data_root, workspace_path)
    if conflict is not None:
        raise IsolationRefused(conflict)

    run_id = procs.new_run_id()
    temp_dir = known.data_root / "tmp" / f"run-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(temp_dir, 0o700)
    markers = procs.RunMarkers.create(known.marker_root, run_id)
    lease: procs.RunLease | None = None
    process: subprocess.Popen | None = None
    try:
        procs.self_test(markers)
        lease = procs.RunLease.create(
            known.data_root, run_id, markers, temp_dir, str(workspace_path)
        )
        policy = RunPolicy(
            workspace=workspace_path,
            temp_dir=temp_dir,
            marker_denied=markers.denied,
            marker_allowed=markers.allowed,
            deny_ports=tuple(sensitive_ports()),
            deny_network=True,
        )
        profile_text = build_profile(policy, known)
        try:
            run_attestation(
                policy,
                profile_text,
                protected=known.protected(),
                sandbox_exec=SANDBOX_EXEC,
            )
        except AttestationFailed as exc:
            raise IsolationRefused(f"Streaming tool isolation self-check failed: {exc}") from exc
        profile_file = temp_dir / "policy.sb"
        profile_file.write_text(profile_text, encoding="utf-8")
        child_env = {**env, "TMPDIR": f"{temp_dir}/"}
        process = subprocess.Popen(  # noqa: S603 - launcher and argv are fixed by caller
            [SANDBOX_EXEC, "-f", str(profile_file), *argv],
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
        lease.update_members()
        return StreamProcess(process, markers, temp_dir, lease)
    except BaseException:
        if process is not None:
            try:
                cleanup = procs.terminate_all(markers)
            except procs.SupervisionUnavailable as exc:
                raise IsolationRefused(
                    "streaming process cleanup failed; its workspace remains locked"
                ) from exc
            if not cleanup.clean:
                raise IsolationRefused(
                    "streaming process cleanup left descendants; its workspace remains locked"
                ) from None
        if lease is not None:
            lease.finish()
        markers.remove()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def status() -> dict:
    """Whether this machine can isolate a run, and why not when it cannot."""

    if sys.platform != "darwin":
        return {
            "supported": False,
            "backend": None,
            "supervision": False,
            "reason": (
                f"no validated sandbox backend for {sys.platform}; external engines are "
                "disabled here"
            ),
        }
    if not Path(SANDBOX_EXEC).exists():
        return {
            "supported": False,
            "backend": None,
            "supervision": False,
            "reason": f"{SANDBOX_EXEC} is missing",
        }
    # Nested sandboxes are refused by the kernel, so a Neo that is itself sandboxed
    # cannot apply a profile to its children. Detected by trying it.
    try:
        probe = subprocess.run(
            [
                SANDBOX_EXEC,
                "-p",
                # A deny is what makes this a real test: nesting succeeds under a
                # bare "(allow default)" and fails under any profile that actually
                # restricts something, which is every profile Neo builds.
                '(version 1)(allow default)(deny file-read* (literal "/dev/null"))',
                "/usr/bin/true",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "supported": False,
            "backend": None,
            "supervision": False,
            "reason": f"sandbox-exec could not be run: {exc}",
        }
    if probe.returncode != 0:
        return {
            "supported": False,
            "backend": None,
            "supervision": False,
            "reason": (
                "sandbox-exec refused to apply a profile here, which happens when Neo "
                "is itself sandboxed: " + (probe.stderr.strip()[:200] or "unknown error")
            ),
        }
    try:
        markers = procs.RunMarkers.create(roots().marker_root, procs.new_run_id())
    except (OSError, procs.SupervisionUnavailable) as exc:
        return {
            "supported": False,
            "backend": "seatbelt",
            "supervision": False,
            "reason": f"process supervision unavailable: {exc}",
        }
    try:
        procs.self_test(markers)
    except procs.SupervisionUnavailable as exc:
        return {
            "supported": False,
            "backend": "seatbelt",
            "supervision": False,
            "reason": f"process supervision unavailable: {exc}",
        }
    finally:
        markers.remove()
    return {"supported": True, "backend": "seatbelt", "supervision": True, "reason": ""}


def require_available() -> None:
    state = status()
    if not state["supported"]:
        raise IsolationUnavailable(state["reason"])


def recover_interrupted_runs() -> int:
    """Recover durable run leases before a workspace or thread state is reused."""

    known = roots()
    records = known.data_root / "isolation" / "runs"
    if not records.exists() or not any(records.glob("*.json")):
        return 0
    require_available()
    return procs.recover_interrupted(known.data_root)


def run(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    purpose: str,
    profile_id: str,
    workspace: str,
    thread_state: str | None = None,
    exec_paths: tuple[str, ...] = (),
    shared_config_paths: list[str] | None = None,
    deny_ports: list[int] | None = None,
    mode: str = "normal",
    timeout: float = 3600.0,
    stdin_text: str | None = None,
    on_line=None,
    is_cancelled=None,
    raw_stdout: bool = False,
    max_stdout_bytes: int | None = None,
) -> RunResult:
    """Run ``argv`` isolated from everything that is not this run's own."""

    require_available()
    exposure = environment.refusal(purpose)
    if exposure is not None:
        raise IsolationUnavailable(exposure)
    recover_interrupted_runs()
    if purpose in {"external_agent", "engine_validation"}:
        network_refusal = private_endpoint_refusal()
        if network_refusal is not None:
            raise IsolationUnavailable(network_refusal)
    known = roots()
    workspace_path = Path(workspace).expanduser().resolve()

    refusal = overlaps_protected(workspace_path, known, profile_id=profile_id)
    if refusal is not None:
        raise IsolationRefused(
            f"This folder cannot be opened by an agent: {refusal}. Pick a project "
            "folder outside Neo's own storage."
        )
    if purpose == "external_agent":
        state_refusal = (
            thread_state_refusal(Path(thread_state), profile_id=profile_id, known=known)
            if thread_state
            else "an external engine needs its own CLI state directory"
        )
        if state_refusal is not None:
            raise IsolationRefused(state_refusal)

    conflict = procs.active_lease_conflict(
        known.data_root,
        workspace_path,
        Path(thread_state).resolve() if thread_state else None,
    )
    if conflict is not None:
        raise IsolationRefused(conflict)

    run_id = procs.new_run_id()
    temp_dir = known.data_root / "tmp" / f"run-{run_id}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(temp_dir, 0o700)
    except OSError:
        pass

    markers = procs.RunMarkers.create(known.marker_root, run_id)
    lease: procs.RunLease | None = None
    try:
        procs.self_test(markers)
    except procs.SupervisionUnavailable as exc:
        markers.remove()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise IsolationUnavailable(str(exc)) from exc

    try:
        lease = procs.RunLease.create(
            known.data_root,
            run_id,
            markers,
            temp_dir,
            str(workspace_path),
            thread_state,
        )
    except BaseException:
        markers.remove()
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    policy = RunPolicy(
        workspace=workspace_path,
        thread_state=Path(thread_state).resolve() if thread_state else None,
        exec_paths=tuple(Path(p).resolve() for p in exec_paths),
        temp_dir=temp_dir,
        marker_denied=markers.denied,
        marker_allowed=markers.allowed,
        deny_ports=tuple(sorted(set(sensitive_ports()) | set(deny_ports or ()))),
        shared_config_paths=tuple(Path(p) for p in (shared_config_paths or ())),
        read_only_workspace=mode == "plan",
        deny_network=purpose in {"fs_helper", "command_sandbox", "git_tooling"},
    )
    profile_text = build_profile(policy, known)

    result = RunResult()
    try:
        try:
            run_attestation(
                policy,
                profile_text,
                protected=known.protected(),
                sandbox_exec=SANDBOX_EXEC,
            )
        except AttestationFailed as exc:
            raise IsolationRefused(
                f"Neo did not start this run because its own isolation self-check "
                f"failed: {exc}"
            ) from exc

        profile_file = temp_dir / "policy.sb"
        profile_file.write_text(profile_text, encoding="utf-8")
        child_env = {**env, "TMPDIR": f"{temp_dir}/"}
        full_argv = [SANDBOX_EXEC, "-f", str(profile_file), *argv]

        result = _spawn(
            full_argv,
            cwd=cwd,
            env=child_env,
            timeout=timeout,
            stdin_text=stdin_text,
            on_line=on_line,
            is_cancelled=is_cancelled,
            on_members=lease.update_members,
            raw_stdout=raw_stdout,
            max_stdout_bytes=max_stdout_bytes,
        )
    finally:
        cleanup = None
        try:
            cleanup = procs.terminate_all(markers)
        except (OSError, procs.SupervisionUnavailable) as exc:
            result.quarantined = True
            result.quarantine_reason = (
                f"this run's processes could not be accounted for: {exc}"
            )
        if cleanup is not None:
            result.killed = cleanup.killed
            result.survivors = cleanup.survivors
            if not cleanup.clean:
                result.quarantined = True
                result.quarantine_reason = (
                    f"{len(cleanup.survivors)} process(es) from this run could not be "
                    "stopped, so its workspace and state stay locked"
                )
        if not result.quarantined:
            lease.finish()
            markers.remove()
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            _LOG.error(
                "isolation_run_quarantined purpose=%s profile=%s reason=%s",
                purpose,
                profile_id,
                result.quarantine_reason,
            )
    return result


def _spawn(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float,
    stdin_text: str | None,
    on_line,
    is_cancelled,
    on_members=None,
    raw_stdout: bool = False,
    max_stdout_bytes: int | None = None,
) -> RunResult:
    result = RunResult()
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built here, never user input
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not raw_stdout,
            bufsize=0 if raw_stdout else 1,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        result.exit_code = None
        result.stderr = f"could not start {argv[0]}: {exc}"
        return result

    if stdin_text is not None and process.stdin is not None:
        def feed() -> None:
            try:
                process.stdin.write(stdin_text)
                process.stdin.close()
            except (OSError, ValueError):
                pass

        threading.Thread(target=feed, name="neo-iso-stdin", daemon=True).start()

    if on_members is not None:
        try:
            on_members()
        except (OSError, procs.SupervisionUnavailable) as exc:
            process.kill()
            result.quarantined = True
            result.quarantine_reason = str(exc)

    stderr_chunks: list[str] = []

    def drain() -> None:
        try:
            for line in process.stderr:
                if sum(len(c) for c in stderr_chunks) < 64 * 1024:
                    stderr_chunks.append(
                        line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
                    )
        except (OSError, ValueError):
            pass

    errors = threading.Thread(target=drain, name="neo-iso-err", daemon=True)
    errors.start()

    stopping = threading.Event()
    state = {"timed_out": False, "cancelled": False}

    def watch() -> None:
        deadline = time.monotonic() + timeout
        while not stopping.wait(1.0):
            if process.poll() is not None:
                return
            if on_members is not None:
                try:
                    on_members()
                except (OSError, procs.SupervisionUnavailable) as exc:
                    result.quarantined = True
                    result.quarantine_reason = f"process membership could not be persisted: {exc}"
                    process.kill()
                    return
            if time.monotonic() >= deadline:
                state["timed_out"] = True
                process.kill()
                return
            if is_cancelled is not None:
                try:
                    if is_cancelled():
                        state["cancelled"] = True
                        process.kill()
                        return
                except Exception:  # pragma: no cover - a probe must not kill a run
                    _LOG.warning("isolation_cancel_probe_failed", exc_info=True)

    watcher = threading.Thread(target=watch, name="neo-iso-watch", daemon=True)
    watcher.start()

    out: list[str] = []
    binary_out: list[bytes] = []
    binary_size = 0
    lines: list[str] = []
    try:
        if raw_stdout:
            while chunk := process.stdout.read(64 * 1024):
                binary_size += len(chunk)
                if max_stdout_bytes is not None and binary_size > max_stdout_bytes:
                    result.output_limit_exceeded = True
                    process.kill()
                    break
                binary_out.append(chunk)
        else:
            for raw in process.stdout:
                line = raw.rstrip("\n")
                out.append(raw)
                lines.append(line)
                if on_line is not None:
                    try:
                        on_line(_parse(line))
                    except Exception:  # pragma: no cover - a parse slip is not fatal
                        _LOG.warning("isolation_on_line_failed", exc_info=True)
    except (OSError, ValueError):
        pass
    finally:
        stopping.set()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        errors.join(timeout=2.0)
        watcher.join(timeout=25.0)
        if watcher.is_alive():
            result.quarantined = True
            result.quarantine_reason = "process membership monitor did not stop"
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    result.exit_code = process.returncode
    result.stdout_bytes = b"".join(binary_out)
    result.stdout = (
        result.stdout_bytes.decode("utf-8", errors="replace") if raw_stdout else "".join(out)
    )
    result.stderr = "".join(stderr_chunks).strip()
    result.timed_out = state["timed_out"]
    result.cancelled = state["cancelled"]
    result.lines = lines
    return result


def _parse(line: str):
    try:
        value = json.loads(line)
    except (ValueError, TypeError):
        return line
    return value if isinstance(value, dict) else line


__all__ = [
    "IsolationRefused",
    "IsolationUnavailable",
    "RunResult",
    "StreamProcess",
    "require_available",
    "recover_interrupted_runs",
    "run",
    "start_stream",
    "status",
]
