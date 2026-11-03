"""One Neo server per data directory, enforced.

A server keeps state that only its own process knows: guest sessions, the chat turn
queue, live model streams (what Stop cancels) and the one-at-a-time decision on a
calendar proposal. Startup also deletes the guest storage it finds, because in a
single server that can only be a dead process's. A second server on the same data,
whether a second instance or ``--workers 2``, would delete the first one's live guest
data and could decide a calendar card twice.

So a server holds an exclusive lock on ``neo-server.lock`` in the data directory for
its whole life. The operating system releases it when the process ends, however it
ends, so a crash leaves nothing stale. Holding it again in the same process (tests,
a second ``create_app``) is a no-op; an app for another data directory in the same
process takes that directory's lock too.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from app.core.paths import IS_WINDOWS

LOCK_NAME = "neo-server.lock"

_guard = threading.Lock()
_held: dict[Path, int] = {}


class AlreadyServing(RuntimeError):
    """Another process is serving this data directory."""


def hold(data_dir: Path) -> None:
    """Take the data directory's server lock for this process, or refuse."""

    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir.resolve() / LOCK_NAME
    with _guard:
        if path in _held:
            return
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if IS_WINDOWS:
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AlreadyServing(
                f"Another Neo server is already using {data_dir}. Stop it first, or give "
                "this one its own data directory (NEO_DATA_DIR). Neo runs as a single "
                "process: do not start it with more than one worker."
            ) from None
        _held[path] = fd
