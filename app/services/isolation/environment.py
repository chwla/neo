"""What a sandboxed run could read from Neo's own environment.

Measured on macOS 26: a run reads any same-user process's environment through
``sysctl(KERN_PROCARGS2)``, and Seatbelt does not mediate that sysctl. Denying every
``process-info*`` operation, or the ``kern.proc*`` sysctls by name, still leaves it
readable. So the environment Neo was *launched* with is visible to every run -- and a
developer who starts Neo from a shell that exports ``OPENAI_API_KEY`` or a tool token
hands that token to whatever an agent runs.

The sandbox cannot close this; Neo can refuse to make it worse. It reads its own
kernel-visible environment -- the same bytes a run would get, not ``os.environ``,
which can differ -- and refuses agent-influenced launches while that environment
holds credential-like variables. Only variable *names* are ever reported.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from collections.abc import Mapping

#: Launches that execute code an agent or a model influenced. Neo's own fixed
#: helpers (the filesystem helper, hardened git) are not here: they read nothing
#: they are sent, and refusing them would break undo and delivery.
AGENT_INFLUENCED = frozenset(
    {"external_agent", "engine_validation", "command_sandbox", "test_runner", "stream"}
)

_CTL_KERN = 1
_KERN_PROCARGS2 = 49

#: Name segments that mean "this holds a credential". Matched on whole segments so
#: ``TOKENIZERS_PARALLELISM`` and ``KEYCHAIN_PATH`` are not flagged.
_CREDENTIAL_SEGMENTS = frozenset({
    "TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "PASSWD", "PASSPHRASE",
    "CREDENTIAL", "CREDENTIALS", "APIKEY", "COOKIE", "KEY",
})
#: Names that match a segment but carry no secret themselves.
_NOT_CREDENTIALS = frozenset({"SSH_AUTH_SOCK", "GPG_AGENT_INFO"})


def credential_names(environ: Mapping[str, str]) -> list[str]:
    """The credential-like variable names in ``environ``, sorted. Values are ignored."""

    flagged: set[str] = set()
    for name in environ:
        upper = name.upper()
        if upper in _NOT_CREDENTIALS or upper.endswith("_SOCK"):
            continue
        segments = {segment for segment in re.split(r"[^A-Z0-9]+", upper) if segment}
        if segments & _CREDENTIAL_SEGMENTS:
            flagged.add(name)
    return sorted(flagged)


def kernel_environment(pid: int) -> dict[str, str] | None:
    """``pid``'s environment as the kernel exposes it, or ``None`` if unreadable.

    The ``KERN_PROCARGS2`` buffer is: a 4-byte argc, the executable path, padding
    NULs, argc argument strings, then the environment strings up to an empty one.
    """

    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, int(pid))
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
    except (OSError, AttributeError, ValueError):
        return None
    raw = buffer.raw[: size.value]
    argc = int.from_bytes(raw[:4], sys.byteorder)
    position = raw.find(b"\0", 4)  # end of the executable path
    if position < 0:
        return None
    while position < len(raw) and raw[position] == 0:
        position += 1
    strings = raw[position:].split(b"\0")
    environment: dict[str, str] = {}
    for item in strings[argc:]:
        if not item:
            break
        name, separator, value = item.partition(b"=")
        if separator:
            environment[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environment


def exposed_credentials() -> list[str]:
    """Credential-like names a run could read from Neo's own environment."""

    environ = kernel_environment(os.getpid())
    if environ is None:
        # Not readable here (not macOS): fall back to what the process holds, which
        # is what a child would inherit and the best available description.
        environ = dict(os.environ)
    return credential_names(environ)


def refusal(purpose: str) -> str | None:
    """Why an agent-influenced launch must not start, or ``None`` if it may."""

    if purpose not in AGENT_INFLUENCED:
        return None
    names = exposed_credentials()
    if not names:
        return None
    return (
        "Neo was started with credentials in its environment ("
        + ", ".join(names)
        + "). On macOS any program a run starts can read the environment of other "
        "programs running as you, and the sandbox cannot prevent it, so Neo will not "
        "start one. Put these values in Neo's .env file instead, which Neo reads "
        "without exporting, and restart Neo from a clean environment, for example "
        "`env -i PATH=\"$PATH\" HOME=\"$HOME\" .venv/bin/python -m uvicorn app.main:app`."
    )


__all__ = [
    "AGENT_INFLUENCED",
    "credential_names",
    "exposed_credentials",
    "kernel_environment",
    "refusal",
]
