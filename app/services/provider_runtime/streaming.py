from __future__ import annotations

import threading
from contextvars import copy_context

_CANCEL: dict[str, threading.Event] = {}


def start(request_id: str, target) -> None:
    cancelled = _CANCEL.setdefault(request_id, threading.Event())
    caller_context = copy_context()

    def run():
        caller_context.run(target, cancelled)

    threading.Thread(target=run, daemon=True, name=f"provider-stream-{request_id[:8]}").start()


def cancel(request_id: str) -> bool:
    if request_id not in _CANCEL:
        return False
    _CANCEL[request_id].set()
    return True


def clear(request_id: str) -> None:
    _CANCEL.pop(request_id, None)


def live(request_ids) -> set[str]:
    """Which of these requests still have a stream thread in this process."""
    return {request_id for request_id in request_ids if request_id in _CANCEL}


def all_live() -> set[str]:
    """Every request with a stream thread in this process, whatever its stored status."""
    return set(_CANCEL)
