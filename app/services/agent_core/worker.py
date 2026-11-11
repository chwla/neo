"""Running a session in the background, durably.

Modelled on the chat generation worker, which is the only refresh-safe executor
Neo had: a daemon thread per unit of work, a database lease with a heartbeat, and
cancellation by flipping the row rather than killing the thread.

It replaces ``ThreadPoolExecutor(max_workers=2)`` from the old runner, which was
a process-global cap of two concurrent runs shared across every profile.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import uuid

from app.services.agent_core import events, store
from app.services.agent_core.completion import CompletionAdjudicator
from app.services.agent_core.loop import AgentLoop, resume_after_approval
from app.services.agent_core.types import EXTERNAL_EXECUTORS, TERMINAL_STATUSES

_LOG = logging.getLogger(__name__)

#: Identifies this process in the lease, so a stale lease can be told apart from
#: one held by a live worker in another process.
PROCESS_WORKER_ID = str(uuid.uuid4())

#: How many heartbeats fit in one lease window.
HEARTBEATS_PER_LEASE = 3

#: Session id -> the profile storage it runs in. Keyed by profile because the
#: concurrency cap is a per-profile preference: counting every profile's runs
#: against each profile's cap let one account's work stall everyone else's.
_ACTIVE: dict[str, str] = {}
#: Sessions that were cancelled while their worker thread is still inside a model
#: call. The thread is still alive -- profile deletion waits on it -- but it no
#: longer holds a turn slot, or one stuck call would block the slot for as long
#: as the provider took to answer.
_RELEASED: set[str] = set()
_LOCK = threading.Lock()


def _profile_key() -> str:
    from app.core.config import active_profile_storage_dir

    return active_profile_storage_dir.get() or ""


def _run_guarded(session_id: str, approval_id: str | None, loop_factory) -> None:
    lease_token = str(uuid.uuid4())
    claimed = store.claim_session(session_id, PROCESS_WORKER_ID, lease_token)
    if claimed is None:
        _LOG.info("agent_session_already_leased session=%s", session_id)
        return
    stop_heartbeat = threading.Event()

    def beat() -> None:
        # A long tool call can outlast the lease window; refreshing from a side
        # thread keeps a live worker from looking abandoned to the next claimant.
        # A heartbeat that cannot be written means the lease will expire and a
        # second worker may claim this run while this one is still going. So a
        # lost lease, or a beat that keeps failing, cancels the run rather than
        # ending this thread silently.
        failures = 0
        while not stop_heartbeat.wait(store.LEASE_SECONDS / HEARTBEATS_PER_LEASE):
            try:
                alive = store.heartbeat(session_id, lease_token)
            except Exception:
                failures += 1
                _LOG.warning("agent_heartbeat_failed session=%s", session_id, exc_info=True)
                if failures < HEARTBEATS_PER_LEASE - 1:
                    continue
                alive = False
            else:
                failures = 0
            if not alive:
                _LOG.warning("agent_lease_lost session=%s", session_id)
                try:
                    store.cancel_session(session_id)
                except Exception:
                    _LOG.warning("agent_lease_cancel_failed session=%s", session_id, exc_info=True)
                return

    heart = threading.Thread(target=beat, name=f"neo-agent-hb-{session_id[:8]}", daemon=True)
    heart.start()
    try:
        # The one place that knows there is more than one kind of engine.
        # Everything around it -- the lease above, the heartbeat, the failure
        # handling below, the cancel-by-row-flip contract -- is identical for
        # both, which is the point of branching here rather than anywhere else.
        executor = str((store.get_session(session_id) or {}).get("executor") or "neo")
        if executor in EXTERNAL_EXECUTORS:
            from app.services import external_agents

            # An external run has no approval mechanism to resume into: neither
            # CLI exposes a per-call approval channel in non-interactive mode
            # (see docs/external-agents/cli-surface.md), so a session of this
            # kind never reaches waiting_approval in the first place.
            external_agents.run(session_id)
        else:
            loop = loop_factory()
            if approval_id:
                resume_after_approval(loop, session_id, approval_id)
            else:
                loop.run(session_id)
    except Exception as exc:
        _LOG.warning("agent_session_failed session=%s", session_id, exc_info=exc)
        session = store.get_session(session_id)
        if session and session["status"] not in TERMINAL_STATUSES:
            store.update_session(
                session_id,
                {
                    "status": "failed",
                    "stop_reason": "failed",
                    "error": str(exc),
                    "summary": f"The run failed: {exc}",
                    "completed_at": store.now_iso(),
                },
            )
            store.append_event(session_id, events.RUN_FAILED, {"detail": str(exc)})
    finally:
        stop_heartbeat.set()
        try:
            store.release_session(session_id, lease_token)
        finally:
            # Even when the release cannot be written -- a run that outlived its
            # deleted profile has no database left -- or the id would hold one of
            # the process's turn slots until restart.
            with _LOCK:
                _ACTIVE.pop(session_id, None)
                _RELEASED.discard(session_id)


def start(session_id: str, *, approval_id: str | None = None, loop_factory=None) -> None:
    """Start (or resume) a session on a background thread.

    The submitting request's context is copied into the thread because Neo
    selects the profile database through a ContextVar. Without the copy the
    worker resolves the base database, finds no such session, and the run sits
    queued forever with nothing logged -- the exact failure the old runner hit.
    """

    with _LOCK:
        if session_id in _ACTIVE:
            return
        _ACTIVE[session_id] = _profile_key()

    factory = loop_factory or default_loop_factory
    context = contextvars.copy_context()
    thread = threading.Thread(
        target=lambda: context.run(_run_guarded, session_id, approval_id, factory),
        name=f"neo-agent-{session_id[:8]}",
        daemon=True,
    )
    thread.start()


def is_active(session_id: str) -> bool:
    with _LOCK:
        return session_id in _ACTIVE


def active_ids() -> set[str]:
    """Every session with a worker thread in this process, whatever its profile.

    For callers that must not touch a run that is still going: profile deletion,
    which waits for these threads, and the lease sweep, which leaves them alone.
    """

    with _LOCK:
        return set(_ACTIVE)


def slot_holders() -> set[str]:
    """The current profile's sessions that are using a turn slot right now.

    Read by the concurrency cap, which has to count a run that has been handed a
    thread but has not yet flipped its row to ``running``.  Counting only the
    database would let a second turn in through that window. Only this profile's
    runs count, and not one that has been cancelled and is merely letting go.
    """

    profile = _profile_key()
    with _LOCK:
        return {
            session_id
            for session_id, owner in _ACTIVE.items()
            if owner == profile and session_id not in _RELEASED
        }


def release_slot(session_id: str) -> None:
    """Give back a cancelled run's turn slot without waiting for its thread."""

    with _LOCK:
        if session_id in _ACTIVE:
            _RELEASED.add(session_id)


def default_loop_factory() -> AgentLoop:
    from app.services.llm import get_llm_client

    def llm() -> object:
        # Generous: a single write_file call carries the whole file body as an
        # argument, and a truncated turn costs an extra round trip to recover.
        return get_llm_client(num_predict=8192, timeout=900, route_name="agent")

    def completion_client() -> object:
        return get_llm_client(num_predict=400, timeout=60, route_name="agent")

    return AgentLoop(
        llm_factory=llm,
        adjudicator=CompletionAdjudicator(completion_client),
        context_window=_context_window(),
    )


def _context_window() -> int | None:
    try:
        from app.services.provider_runtime.router import select

        return (select("chat", "agent").get("model_record") or {}).get("context_window")
    except Exception:
        return None
