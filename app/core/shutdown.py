"""One flag every long-lived response watches, so stopping Neo actually stops it.

A browser tab holds ``/api/chat-events`` open for as long as it is visible. A
server that is asked to stop waits for open connections to finish, and that one
never does on its own -- so Ctrl+C sat on "Waiting for connections to close" until
it was killed, and ``docker stop`` ended in SIGKILL with the clean-up skipped.

The flag is set as soon as the stop signal arrives (the server's own shutdown
hook runs only *after* the connections have closed, which is too late), and each
streaming loop checks it between polls and ends with a ``server.shutdown`` record
the interface uses to back off before reconnecting.
"""

from __future__ import annotations

import json
import signal
import threading

_REQUESTED = threading.Event()

#: The last record a stream sends when it ends because the server is stopping.
SHUTDOWN_EVENT = {"type": "server.shutdown"}
SHUTDOWN_LINE = json.dumps(SHUTDOWN_EVENT) + "\n"


def begin() -> None:
    _REQUESTED.set()


def reset() -> None:
    _REQUESTED.clear()


def requested() -> bool:
    return _REQUESTED.is_set()


def wait(timeout: float) -> bool:
    """Sleep up to ``timeout`` seconds; true as soon as a stop is requested."""

    return _REQUESTED.wait(timeout)


def watch_signals() -> None:
    """Set the flag when the process is told to stop, then let the server react.

    Chained in front of whatever handler is installed (the ASGI server's own), so
    its graceful shutdown still runs. Signal handlers can only be installed from
    the main thread; anywhere else -- a test client, an embedded app -- this is a
    no-op and the lifespan hook alone sets the flag.
    """

    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    for name in names:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous = signal.getsignal(number)
            if getattr(previous, "_neo_shutdown_watch", False):
                continue

            def handler(signum, frame, _previous=previous):
                begin()
                if callable(_previous):
                    _previous(signum, frame)
                elif _previous == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    signal.raise_signal(signum)

            handler._neo_shutdown_watch = True  # type: ignore[attr-defined]
            signal.signal(number, handler)
        except (ValueError, OSError):
            return
