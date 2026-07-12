from __future__ import annotations

import threading

_CANCEL: dict[str, threading.Event] = {}


def start(request_id: str, target) -> None:
    cancelled = _CANCEL.setdefault(request_id, threading.Event())

    def run():
        target(cancelled)

    threading.Thread(target=run, daemon=True, name=f"provider-stream-{request_id[:8]}").start()


def cancel(request_id: str) -> bool:
    if request_id not in _CANCEL:
        return False
    _CANCEL[request_id].set()
    return True


def clear(request_id: str) -> None:
    _CANCEL.pop(request_id, None)
