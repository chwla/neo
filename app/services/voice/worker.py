"""One thread for transcription, and a bound on how much can be waiting for it.

FastAPI runs a plain ``def`` route in anyio's threadpool, which is the right way to
*enter* blocking work but the wrong place to *do* this particular blocking work. Two
reasons, and both bite on a laptop rather than in theory:

*Nothing bounds that pool for this purpose.* Forty concurrent transcriptions would
thrash the machine into swap, and they would be competing for it with the chat
generation workers, which share the same pool.

*CTranslate2 is already internally multithreaded.* N concurrent decodes, each asking
for ``cpu_threads`` cores, is oversubscription that makes total throughput collapse --
every job gets slower and none finish sooner.

So transcription is serialised through a single worker with a short queue, and a
request that arrives past the queue bound is refused rather than silently held. Waiting
on the future does occupy the caller's threadpool slot, which is acceptable precisely
because the queue is bounded: the wait is seconds and bounded in number.

CTranslate2 releases the GIL during inference, so this one worker genuinely runs
alongside the event loop rather than blocking it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

T = TypeVar("T")

# How many jobs may wait. Three is enough to absorb a finalize landing while a partial
# is running; past that the honest answer is "too busy" rather than a growing queue the
# user experiences as the feature having hung.
MAX_QUEUED = 3


class Busy(RuntimeError):
    """The engine already has as much work as it will hold."""


class VoiceWorker:
    """A single-threaded executor with a queue bound and per-key coalescing."""

    def __init__(self, max_queued: int = MAX_QUEUED) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
        self._lock = threading.Lock()
        self._queued = 0
        self._max_queued = max_queued
        # The newest pending partial per session, so a superseded one can be dropped
        # rather than decoded and thrown away.
        self._pending: dict[str, int] = {}

    def submit(self, job: Callable[[], T]) -> Future[T]:
        """Queue work, refusing rather than growing without limit."""

        with self._lock:
            if self._queued >= self._max_queued:
                raise Busy("The speech engine is busy.")
            self._queued += 1

        def run() -> T:
            try:
                return job()
            finally:
                with self._lock:
                    self._queued -= 1

        return self._pool.submit(run)

    def submit_partial(self, key: str, generation: int, job: Callable[[], T]) -> Future[T | None]:
        """Queue a partial that a newer one for the same key may cancel.

        Partials are provisional by definition, so decoding a stale one is pure waste --
        its text would be discarded the moment it arrived. Recording the newest
        generation lets the job check, at the moment it finally starts, whether anyone
        still wants it.
        """

        with self._lock:
            self._pending[key] = generation

        def run() -> T | None:
            with self._lock:
                if self._pending.get(key, generation) != generation:
                    return None
            return job()

        return self.submit(run)

    def forget(self, key: str) -> None:
        """Drop any pending partial for a session, so a finalize does not wait behind it."""

        with self._lock:
            self._pending.pop(key, None)

    @property
    def depth(self) -> int:
        with self._lock:
            return self._queued

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


_worker: VoiceWorker | None = None
_worker_lock = threading.Lock()


def worker() -> VoiceWorker:
    """The process-wide worker."""

    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = VoiceWorker()
        return _worker


def reset_worker() -> Any:
    """Drop the worker, for tests that need a clean queue."""

    global _worker
    with _worker_lock:
        if _worker is not None:
            _worker.shutdown()
        _worker = None
