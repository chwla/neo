"""Periodic background sweep that marks calendar reminders due.

No scheduler library exists anywhere in Neo (no APScheduler/celery/cron), and
every profile has its own SQLite database selected through a contextvar
(``app.services.profile_accounts.profile_database``), so a single global timer
isn't enough by itself -- each sweep cycle has to visit every profile in turn.
This thread only *marks* reminders due (writes a
``calendar_reminder_deliveries`` row); it never pushes anything anywhere.
Delivery to a browser is the frontend polling ``GET /calendar/reminders/pending``
while a profile is signed in (see ``app/api/routes/calendar.py``).
"""

from __future__ import annotations

import logging
import os
import threading

from app.services.calendar.service import CalendarService
from app.services.calendar.store import initialize_calendar_tables
from app.services.profile_accounts import list_profiles, profile_database

_LOG = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 30.0

_started = threading.Event()


def _sweep_once() -> None:
    for profile in list_profiles():
        try:
            with profile_database(profile["id"]):
                # A profile provisioned before this feature shipped only gets
                # the table on its next login (`_initialize_profile_storage`).
                # The sweep runs independently of logins, so it has to be able
                # to create its own table too; `CREATE TABLE IF NOT EXISTS` is
                # a cheap no-op once the profile has logged in since.
                initialize_calendar_tables()
                CalendarService().due_reminders()
        except Exception:
            _LOG.warning("calendar_reminder_sweep_failed profile=%s", profile["id"], exc_info=True)


def _run(stop_event: threading.Event) -> None:
    while True:
        _sweep_once()
        if stop_event.wait(SWEEP_INTERVAL_SECONDS):
            return


def start_reminder_sweep() -> None:
    """Start the sweep thread once per process.

    A no-op under pytest. ``create_app()`` runs once per real server process
    but many times per test process (each ``TestClient``-based fixture calls
    it fresh), and this thread would otherwise outlive the test that started
    it -- running forever against whatever env-derived paths happen to be
    live at each tick, well after that test's ``monkeypatch`` reverted them.
    That is exactly the kind of leak ``tests/fsguard.py`` exists to catch.
    """

    if _started.is_set() or os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return
    _started.set()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run, args=(stop_event,), name="neo-calendar-reminder-sweep", daemon=True
    )
    thread.start()
