"""Programs started without a sandbox, so that stopping one stops everything it started.

Used for the runs a person approved without a sandbox. Ending only the program Neo
started left whatever it had started still running: a grandchild holding the output
pipe kept Neo reading until it exited on its own, and on Windows, where a timed-out
``subprocess.run`` waits for that output with no limit, the run never returned.

POSIX starts the program in a new session, so its descendants share its process
group unless they leave it. Windows starts it suspended and puts it in a job object
before it runs, so every descendant is in the job, even one whose parent has exited.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any

from app.core.paths import IS_WINDOWS

_LOG = logging.getLogger(__name__)

_CREATE_SUSPENDED = 0x4


class ProcessTree:
    """``subprocess.Popen`` for ``argv``, plus a way to end it with its descendants."""

    def __init__(self, argv: list[str], **popen_kwargs: Any) -> None:
        self._job = None
        if not IS_WINDOWS:
            self.process = subprocess.Popen(argv, start_new_session=True, **popen_kwargs)
            return
        flags = popen_kwargs.pop("creationflags", 0) | _CREATE_SUSPENDED
        self.process = subprocess.Popen(argv, creationflags=flags, **popen_kwargs)
        try:
            self._job = _windows_job_for(self.process)
        finally:
            _windows_resume(self.process)

    def kill(self) -> None:
        """End the program and every descendant still in its group or job."""

        if IS_WINDOWS:
            if self._job is not None:
                _windows_terminate_job(self._job)
            else:
                # No job: walk the tree by parent process while the root is alive.
                subprocess.run(  # noqa: S603 - fixed argv
                    ["taskkill", "/T", "/F", "/PID", str(self.process.pid)],
                    capture_output=True,
                    check=False,
                )
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self.process.kill()

    def close(self) -> None:
        if self._job is not None:
            _windows_close(self._job)
            self._job = None


def run_bounded(
    argv: list[str], *, cwd: Any, env: dict[str, str], timeout: float
) -> tuple[str, str, int | None, bool]:
    """Run to completion or ``timeout`` seconds: (stdout, stderr, exit code, timed out).

    At the limit the whole tree is ended and the output read so far is returned.
    Output is read as UTF-8, as the test runner reads it: decoding by the locale meant
    cp1252 on Windows, which garbled what git, Node or pytest wrote and failed outright
    on some characters.
    """

    tree = ProcessTree(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    try:
        try:
            stdout, stderr = tree.process.communicate(timeout=timeout)
            return stdout or "", stderr or "", tree.process.returncode, False
        except subprocess.TimeoutExpired:
            tree.kill()
        try:
            stdout, stderr = tree.process.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            # Something outside the tree still holds the output; stop waiting for it.
            _LOG.warning("process_tree_output_still_open pid=%s", tree.process.pid)
            stdout, stderr = _text(exc.stdout), _text(exc.stderr)
        return stdout or "", stderr or "", None, True
    finally:
        tree.close()


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel.TerminateJobObject.restype = wintypes.BOOL
    kernel.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    return ctypes, kernel


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


def _kill_on_job_close(ctypes, kernel, job) -> None:
    """Have Windows end the job's processes when Neo's handle to it closes.

    That handle closes when Neo exits however it exits -- a crash included -- so a
    test suite or a build Neo started cannot outlive it.
    """

    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class IoCounters(ctypes.Structure):
        _fields_ = tuple((name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        ))

    class ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", BasicLimits),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(
        job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        _LOG.warning("process_tree_job_limits_failed error=%s", ctypes.get_last_error())


def _windows_job_for(process: subprocess.Popen):
    """A new job holding ``process``, or ``None`` (logged) if Windows refused one."""

    ctypes, kernel = _kernel32()
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        _LOG.warning("process_tree_job_unavailable error=%s", ctypes.get_last_error())
        return None
    _kill_on_job_close(ctypes, kernel, job)
    if not kernel.AssignProcessToJobObject(job, int(process._handle)):  # type: ignore[attr-defined]
        _LOG.warning("process_tree_job_assign_failed error=%s", ctypes.get_last_error())
        kernel.CloseHandle(job)
        return None
    return job


def _windows_resume(process: subprocess.Popen) -> None:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.restype = ctypes.c_long
    ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    status = ntdll.NtResumeProcess(int(process._handle))  # type: ignore[attr-defined]
    if status != 0:
        process.kill()
        raise OSError(f"could not resume {process.args!r} (NTSTATUS {status & 0xFFFFFFFF:#x})")


def _windows_terminate_job(job) -> None:
    ctypes, kernel = _kernel32()
    if not kernel.TerminateJobObject(job, 1):
        _LOG.warning("process_tree_terminate_failed error=%s", ctypes.get_last_error())


def _windows_close(job) -> None:
    _, kernel = _kernel32()
    kernel.CloseHandle(job)
