"""Finding and ending every process a run started, including the ones that hide.

A coding agent spawns shells, test runners and language servers, and those spawn
more. Killing the process group is not enough: anything that calls ``setsid`` leaves
the group, and a double-forked child is reparented to launchd, so an empty group
proves nothing. A survivor still holds the sandbox it inherited, but it also still
holds the run's workspace and thread state -- so releasing those, or offering to undo
the run, while one is alive is what this module exists to prevent.

How membership is decided
-------------------------
Each run gets two empty marker files. Its sandbox profile denies reading one and
allows the other. ``sandbox_check`` then answers, for any pid, "would that process be
refused this path?" -- and a process is a member of this run exactly when it is
refused the denied marker *and* allowed the sibling.

Both halves are load-bearing, and the second was found the hard way: with one marker,
every App-Sandboxed process on the machine (Chrome's helpers, Discord, Apple's own
daemons -- around 400 of them here) denies an unknown path and looks like a member. A
kill loop built on the one-marker test would have been catastrophic. The sibling check
removes all of them, because they deny *both* markers while a real member denies only
the first.

Identity is pid plus start time, re-checked immediately before every signal, so a pid
that has been recycled onto an unrelated process is never signalled.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

IS_DARWIN = sys.platform == "darwin"

#: ``sandbox_check`` filter and flag values from Apple's sandbox.h.
_SANDBOX_FILTER_PATH = 1
_SANDBOX_CHECK_NO_REPORT = 0x40000000
#: ``proc_pidinfo`` flavour for the BSD short info struct.
_PROC_PIDTBSDINFO = 3
_RUN_ID = re.compile(r"^[0-9a-f]{16}$")


class SupervisionUnavailable(RuntimeError):
    """Run membership cannot be determined on this machine.

    Fail closed: without it a run's survivors could not be found, and the contract
    does not allow releasing a workspace while one might still be writing to it.
    """


class _BSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_sec: int
    start_usec: int
    comm: str
    uid: int

    def key(self) -> tuple[int, int, int]:
        return (self.pid, self.start_sec, self.start_usec)


class _Libc:
    """The two system calls this needs, with argument types pinned.

    ``sandbox_check`` is variadic: the path is passed after three fixed arguments.
    Declaring only those three in ``argtypes`` is what makes the path reach it
    correctly on arm64 -- with a four-argument ``argtypes`` it is passed in the wrong
    place and the answer is meaningless (it reported every process a member).
    """

    def __init__(self) -> None:
        self._lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        self.sandbox_check = self._lib.sandbox_check
        self.sandbox_check.restype = ctypes.c_int
        self.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        self.proc_pidinfo = self._lib.proc_pidinfo
        self.proc_pidinfo.restype = ctypes.c_int
        self.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]


_LIBC: _Libc | None = None


def _libc() -> _Libc:
    global _LIBC
    if _LIBC is None:
        if not IS_DARWIN:
            raise SupervisionUnavailable("run membership needs macOS sandbox_check")
        try:
            _LIBC = _Libc()
        except (OSError, AttributeError) as exc:
            raise SupervisionUnavailable(f"sandbox_check unavailable: {exc}") from exc
    return _LIBC


def identity(pid: int) -> ProcessIdentity | None:
    """The identity of ``pid``, or ``None`` if it is gone or opaque to us."""

    try:
        lib = _libc()
    except SupervisionUnavailable:
        return None
    info = _BSDInfo()
    size = lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    if size != ctypes.sizeof(info):
        return None
    return ProcessIdentity(
        pid=pid,
        start_sec=int(info.pbi_start_tvsec),
        start_usec=int(info.pbi_start_tvusec),
        comm=info.pbi_comm.decode("utf-8", "replace"),
        uid=int(info.pbi_uid),
    )


@dataclass
class RunMarkers:
    """The two marker files that identify one run's processes."""

    denied: Path
    allowed: Path

    @classmethod
    def create(cls, marker_root: Path, run_id: str) -> RunMarkers:
        marker_root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(marker_root, 0o700)
        except OSError:
            pass
        denied = marker_root / f"{run_id}-d"
        allowed = marker_root / f"{run_id}-a"
        for path in (denied, allowed):
            path.touch(mode=0o600, exist_ok=True)
        return cls(denied=denied, allowed=allowed)

    def remove(self) -> None:
        for path in (self.denied, self.allowed):
            try:
                path.unlink()
            except OSError:
                pass


@dataclass
class RunLease:
    """Durable evidence of a run, written before the child can start."""

    path: Path
    run_id: str
    markers: RunMarkers
    temp_dir: Path
    workspace: str
    thread_state: str | None = None

    @classmethod
    def create(
        cls,
        data_root: Path,
        run_id: str,
        markers: RunMarkers,
        temp_dir: Path,
        workspace: str,
        thread_state: str | None = None,
    ) -> RunLease:
        import fcntl

        if not _RUN_ID.fullmatch(run_id):
            raise SupervisionUnavailable("invalid isolation run identifier")
        directory = data_root / "isolation" / "runs"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        workspace = str(Path(workspace).resolve())
        thread_state = str(Path(thread_state).resolve()) if thread_state else None
        lock_fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            conflict = active_lease_conflict(
                data_root,
                Path(workspace),
                Path(thread_state) if thread_state else None,
            )
            if conflict is not None:
                raise SupervisionUnavailable(conflict)
            path = directory / f"{run_id}.json"
            if path.exists():
                raise SupervisionUnavailable("isolation run identifier already exists")
            lease = cls(path, run_id, markers, temp_dir, workspace, thread_state)
            lease._write(())
            return lease
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def _write(self, identities: tuple[ProcessIdentity, ...]) -> None:
        owner = identity(os.getpid())
        if owner is None:
            raise SupervisionUnavailable("Neo's process identity could not be verified")
        body = json.dumps(
            {
                "version": 1,
                "run_id": self.run_id,
                "owner_pid": os.getpid(),
                "owner_start_sec": owner.start_sec,
                "owner_start_usec": owner.start_usec,
                "workspace": self.workspace,
                "thread_state": self.thread_state,
                "members": [
                    {"pid": item.pid, "start_sec": item.start_sec, "start_usec": item.start_usec}
                    for item in identities
                ],
            },
            sort_keys=True,
        ).encode("utf-8")
        temporary = self.path.with_name(f".{self.run_id}-{secrets.token_hex(4)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def update_members(self) -> None:
        self._write(tuple(members(self.markers)))

    def finish(self) -> None:
        self.path.unlink(missing_ok=True)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def recover_interrupted(data_root: Path) -> int:
    """Kill orphaned sandbox members before their workspaces can be reused."""

    directory = data_root / "isolation" / "runs"
    if not directory.exists():
        return 0
    recovered = 0
    for path in sorted(directory.glob("*.json")):
        run_id = path.stem
        if not _RUN_ID.fullmatch(run_id):
            raise SupervisionUnavailable(f"invalid isolation recovery record: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SupervisionUnavailable(
                f"unreadable isolation recovery record: {path.name}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise SupervisionUnavailable(f"invalid isolation recovery record: {path.name}")
        owner = identity(int(payload.get("owner_pid") or 0))
        if owner is not None and (
            owner.start_sec,
            owner.start_usec,
        ) == (payload.get("owner_start_sec"), payload.get("owner_start_usec")):
            continue
        markers = RunMarkers(
            data_root / "run-markers" / f"{run_id}-d",
            data_root / "run-markers" / f"{run_id}-a",
        )
        if not markers.denied.exists() or not markers.allowed.exists():
            raise SupervisionUnavailable(f"isolation markers missing for {run_id}")
        self_test(markers)
        outcome = terminate_all(markers)
        if not outcome.clean:
            raise SupervisionUnavailable(
                f"{len(outcome.survivors)} sandbox member(s) survived recovery for {run_id}"
            )
        path.unlink()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        markers.remove()
        import shutil

        shutil.rmtree(data_root / "tmp" / f"run-{run_id}", ignore_errors=True)
        recovered += 1
    return recovered


def active_lease_conflict(
    data_root: Path, workspace: Path, thread_state: Path | None = None
) -> str | None:
    """Refuse reuse while another run or a quarantined run owns the resource."""

    workspace = workspace.resolve()
    thread_state = thread_state.resolve() if thread_state else None
    directory = data_root / "isolation" / "runs"
    if not directory.exists():
        return None
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SupervisionUnavailable(
                f"unreadable isolation record blocks workspace reuse: {path.name}"
            ) from exc
        if not isinstance(payload, dict):
            raise SupervisionUnavailable(f"invalid isolation record blocks reuse: {path.name}")
        for field in ("workspace", "thread_state"):
            held = payload.get(field)
            if held is None and field == "thread_state":
                continue
            if not isinstance(held, str) or not Path(held).is_absolute():
                raise SupervisionUnavailable(
                    f"invalid isolation resource blocks reuse: {path.name}"
                )
            held_path = Path(held).resolve()
            for requested in (workspace, thread_state):
                if requested is not None and (
                    requested == held_path or requested in held_path.parents
                    or held_path in requested.parents
                ):
                    name = "thread state" if field == "thread_state" else "workspace"
                    return f"{name} is held by isolation run {path.stem}"
    return None


def _check(lib: _Libc, pid: int, path: Path) -> int:
    return lib.sandbox_check(
        pid,
        b"file-read-data",
        _SANDBOX_FILTER_PATH | _SANDBOX_CHECK_NO_REPORT,
        ctypes.c_char_p(str(path).encode()),
    )


def self_test(markers: RunMarkers) -> None:
    """Prove the oracle works here before anything relies on it to kill processes.

    This process is not a member of the run, so it must be reported as *not* a
    member. If that comes back wrong the oracle is broken, and a broken oracle must
    never reach the kill path.
    """

    lib = _libc()
    if is_member(lib, os.getpid(), markers):
        raise SupervisionUnavailable(
            "the run-membership oracle reported this process as a member of a run "
            "it did not start; refusing to use it"
        )
    if _check(lib, os.getpid(), markers.allowed) != 0:
        raise SupervisionUnavailable(
            "the run-membership oracle could not read a marker it should allow"
        )


def is_member(lib: _Libc, pid: int, markers: RunMarkers) -> bool:
    """Whether ``pid`` is inside this run's sandbox.

    A member is refused the denied marker and allowed its sibling. The second half
    excludes every other sandboxed process on the machine, which refuses both.
    """

    if _check(lib, pid, markers.denied) != 1:
        return False
    return _check(lib, pid, markers.allowed) == 0


def _all_pids() -> list[int]:
    import subprocess

    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid="], capture_output=True, text=True, timeout=20
        )
        if result.returncode != 0:
            raise SupervisionUnavailable("process enumeration failed")
        out = result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SupervisionUnavailable(f"could not enumerate processes: {exc}") from exc
    pids = []
    for token in out.split():
        try:
            pids.append(int(token))
        except ValueError as exc:
            raise SupervisionUnavailable("invalid process inventory") from exc
    if not pids or any(pid <= 0 for pid in pids) or os.getpid() not in pids:
        raise SupervisionUnavailable("incomplete process inventory")
    return pids


def members(markers: RunMarkers, *, exclude: set[int] | None = None) -> list[ProcessIdentity]:
    """Every live process belonging to this run."""

    lib = _libc()
    exclude = exclude or set()
    found: list[ProcessIdentity] = []
    uid = os.getuid()
    for pid in _all_pids():
        if pid in exclude or pid <= 1:
            continue
        if not is_member(lib, pid, markers):
            continue
        who = identity(pid)
        if who is None or who.uid != uid:
            # Not ours, or it exited between the listing and the lookup.
            continue
        found.append(who)
    return found


@dataclass
class CleanupResult:
    killed: tuple[ProcessIdentity, ...] = ()
    survivors: tuple[ProcessIdentity, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.survivors


def terminate_all(
    markers: RunMarkers,
    *,
    exclude: set[int] | None = None,
    grace_seconds: float = 5.0,
) -> CleanupResult:
    """End every member of this run, then confirm none is left.

    Each signal is sent only after re-reading the target's identity and confirming it
    is still the same process and still a member, so a pid recycled onto something
    unrelated between the listing and the kill is never touched.
    """

    lib = _libc()
    killed: list[ProcessIdentity] = []
    targets = {who.key(): who for who in members(markers, exclude=exclude)}

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not targets:
            break
        for key, who in list(targets.items()):
            current = identity(who.pid)
            if current is None or current.key() != key:
                targets.pop(key, None)
                continue
            if not is_member(lib, who.pid, markers):
                targets.pop(key, None)
                continue
            try:
                os.kill(who.pid, sig)
                killed.append(who)
            except ProcessLookupError:
                targets.pop(key, None)
            except OSError as exc:
                _LOG.warning("isolation_kill_failed pid=%s sig=%s err=%s", who.pid, sig, exc)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            remaining = {}
            for key, who in targets.items():
                current = identity(who.pid)
                if current is not None and current.key() == key:
                    remaining[key] = who
            targets = remaining
            if not targets:
                break
            time.sleep(0.1)

    # One last sweep: a member may have spawned something after the listing.
    late = {who.key(): who for who in members(markers, exclude=exclude)}
    return CleanupResult(killed=tuple(killed), survivors=tuple(late.values()))


def new_run_id() -> str:
    return secrets.token_hex(8)


__all__ = [
    "CleanupResult",
    "IS_DARWIN",
    "ProcessIdentity",
    "RunMarkers",
    "RunLease",
    "active_lease_conflict",
    "SupervisionUnavailable",
    "identity",
    "members",
    "new_run_id",
    "recover_interrupted",
    "self_test",
    "terminate_all",
]
