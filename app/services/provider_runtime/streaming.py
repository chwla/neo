from __future__ import annotations

import threading
from contextvars import copy_context

_CANCEL: dict[str, threading.Event] = {}
_UPDATED: dict[str, threading.Event] = {}


def start(request_id: str, target) -> None:
    cancelled = _CANCEL.setdefault(request_id, threading.Event())
    _UPDATED[request_id] = threading.Event()
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
    updated = _UPDATED.pop(request_id, None)
    if updated is not None:
        updated.set()


def notify(request_id: str) -> None:
    """Wake the local consumer after a stream snapshot has been persisted."""
    updated = _UPDATED.get(request_id)
    if updated is not None:
        updated.set()


def wait_for_update(request_id: str, timeout: float = 0.25) -> None:
    """Wait for content/completion; the timeout also discovers external cancellation.

    The reader fetches its snapshot *after* this wait, so coalescing signals or
    a write racing with clear cannot hide any already-persisted content.
    """
    updated = _UPDATED.get(request_id)
    if updated is not None:
        updated.wait(timeout)
        updated.clear()


def live(request_ids) -> set[str]:
    """Which of these requests still have a stream thread in this process."""
    return {request_id for request_id in request_ids if request_id in _CANCEL}


def all_live() -> set[str]:
    """Every request with a stream thread in this process, whatever its stored status."""
    return set(_CANCEL)
