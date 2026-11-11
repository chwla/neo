"""Settling agent runs that no worker is advancing any more.

A run's row says ``running`` for as long as its worker lives. When the process
dies mid-run the row keeps saying so, and nothing ever corrected it for a profile:
startup recovery ran once, against the base database only, and every profile keeps
its runs in its own. The chat showed a run in progress indefinitely.

Two passes fix that. At startup every profile's database is visited and every
running row is marked interrupted -- this process holds the data directory's lock,
so no other one can be working on them. After that a sweep, at the cadence the
calendar reminder sweep already uses, catches a run whose worker died without the
process dying (a heartbeat that stopped): its lease expires and it is marked the
same way, with a ``run.interrupted`` event so an open chat updates.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from app.core.config import get_settings
from app.services.agent_core import events, store
from app.services.profile_accounts import database_url_for, list_profiles, profile_database

_LOG = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0

_started = threading.Event()


def _settle(*, only_stale: bool) -> int:
    """Interrupt abandoned runs in the database in context and announce each."""

    from app.services.agent_core import worker

    interrupted = store.interrupt_abandoned_sessions(
        only_stale=only_stale, keep=worker.active_ids()
    )
    for session_id in interrupted:
        try:
            session = store.get_session(session_id) or {}
            if session.get("anchor_message_id"):
                store.update_anchor_message(
                    session["anchor_message_id"], {"content": store.INTERRUPTED_DETAIL}
                )
            store.append_event(
                session_id,
                events.RUN_INTERRUPTED,
                {"stop_reason": "interrupted", "summary": store.INTERRUPTED_DETAIL},
            )
        except Exception:
            _LOG.warning("agent_interrupt_event_failed session=%s", session_id, exc_info=True)
    return len(interrupted)


def _database_exists(url: str) -> bool:
    return url.startswith("sqlite:///") and Path(url.removeprefix("sqlite:///")).is_file()


def _each_profile(*, only_stale: bool) -> int:
    total = 0
    try:
        profiles = list_profiles()
    except Exception:
        _LOG.warning("agent_recovery_profiles_unreadable", exc_info=True)
        return 0
    for profile in profiles:
        # Never creates a database: connecting to a path that is not there would
        # leave an empty file behind for a profile that has none yet.
        if not _database_exists(database_url_for(profile["id"])):
            continue
        try:
            with profile_database(profile["id"]):
                total += _settle(only_stale=only_stale)
        except Exception:
            _LOG.warning("agent_recovery_failed profile=%s", profile["id"], exc_info=True)
    return total


def recover_all_profiles_at_startup() -> int:
    """Mark every run a previous process left running, in every database."""

    total = 0
    if _database_exists(get_settings().database_url):
        try:
            total += _settle(only_stale=False)
        except Exception:
            _LOG.warning("agent_recovery_failed profile=base", exc_info=True)
    total += _each_profile(only_stale=False)
    if total:
        _LOG.info("agent_sessions_interrupted_at_startup count=%s", total)
    return total


def sweep_expired_leases() -> int:
    """Interrupt runs whose worker stopped heartbeating, in every profile."""

    return _each_profile(only_stale=True)


def _run(stop_event: threading.Event) -> None:
    while not stop_event.wait(SWEEP_INTERVAL_SECONDS):
        try:
            sweep_expired_leases()
        except Exception:
            _LOG.warning("agent_lease_sweep_failed", exc_info=True)


def start_lease_sweep() -> None:
    """Start the sweep thread once per process (a no-op under pytest)."""

    if _started.is_set() or os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return
    _started.set()
    threading.Thread(
        target=_run, args=(threading.Event(),), name="neo-agent-lease-sweep", daemon=True
    ).start()
