"""DNS-rebinding defence: pin the addresses a resolved hostname is allowed to use.

Used by anything that fetches a remote URL after validating its address (e.g.
``app.services.search.content``, fetching a web page for ``web_fetch``).
Resolving a hostname and then handing the *hostname* to the HTTP client lets
the client resolve it again at connect time -- nothing ties the two lookups
together, so a short-TTL attacker record could answer differently for
validation than for the actual connection. Pinning by address rather than
rewriting the URL to an IP keeps the hostname in the URL, so TLS SNI,
certificate verification and Host all still work.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager

_system_getaddrinfo = socket.getaddrinfo
_pins = threading.local()


def _active_pins() -> dict:
    pins = getattr(_pins, "map", None)
    if pins is None:
        pins = {}
        _pins.map = pins
    return pins


def _pinned_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):  # noqa: A002
    """Resolve through the thread's pin map, falling back to the system resolver."""

    addresses = _active_pins().get(str(host).rstrip(".").lower())
    if addresses is None:
        return _system_getaddrinfo(host, port, family, type, proto, flags)

    results = []
    for address in addresses:
        af = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC) and family != af:
            continue
        sockaddr = (
            (str(address), port or 0)
            if af == socket.AF_INET
            else (str(address), port or 0, 0, 0)
        )
        results.append(
            (af, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", sockaddr)
        )
    if not results:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return results


if socket.getaddrinfo is not _pinned_getaddrinfo:
    socket.getaddrinfo = _pinned_getaddrinfo


@contextmanager
def pin_addresses(hostname: str, addresses):
    """Restrict resolution of ``hostname`` to ``addresses`` for the calling thread only.

    Thread-local by design: a pin taken for one in-flight request must not change how
    any other thread resolves the same name.
    """

    key = str(hostname).rstrip(".").lower()
    pins = _active_pins()
    if not key or not addresses:
        yield
        return
    previous = pins.get(key)
    pins[key] = list(addresses)
    try:
        yield
    finally:
        if previous is None:
            pins.pop(key, None)
        else:
            pins[key] = previous
