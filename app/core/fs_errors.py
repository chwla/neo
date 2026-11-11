"""Saying why a file could not be written, in terms of the machine Neo is on.

Every ``PermissionError`` used to be explained with one sentence about container
user ids (``NEO_UID``, ``id -u``). That is the right advice in a container whose
bind mount belongs to another user, and nonsense everywhere else: on Windows the
usual cause is that the file is open in another program or marked read-only, and
on a native Linux or macOS install it is the file's own owner and mode.
"""

from __future__ import annotations

import os
import stat

from app.core.paths import IS_WINDOWS, in_container

_ERROR_SHARING_VIOLATION = 32
_ERROR_LOCK_VIOLATION = 33


def permission_message(relative: str, exc: BaseException | None = None, *, target=None) -> str:
    """A sentence a person can act on for a refused write to ``relative``."""

    if in_container():
        return (
            f"Permission denied writing {relative}. Neo is running in a container, and "
            "the mounted folder belongs to a different user than the container runs "
            "as -- set NEO_UID and NEO_GID to your own (`id -u` / `id -g`) and restart."
        )
    if IS_WINDOWS:
        code = getattr(exc, "winerror", None)
        if code in {_ERROR_SHARING_VIOLATION, _ERROR_LOCK_VIOLATION}:
            return (
                f"{relative} is open in another program, which has it locked. Close it "
                "there and try again. The file was not changed."
            )
        return (
            f"Windows refused the write to {relative}: the file is read-only, is open "
            "in another program, or your account may not change it. The file was not "
            "changed."
        )
    detail = ""
    if target is not None:
        try:
            info = os.stat(target)
            detail = (
                f" It is owned by user {info.st_uid} with mode "
                f"{stat.S_IMODE(info.st_mode):04o}; Neo runs as user {os.getuid()}."
            )
        except OSError:
            detail = ""
    return f"Permission denied writing {relative}.{detail} The file was not changed."
