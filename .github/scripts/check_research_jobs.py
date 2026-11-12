"""Research jobs: every run ends in a stored terminal status, and nothing outlives a cancel.

* A run's outcome is persisted; an engine crash or a profile that cannot open is recorded as
  failed, and the job's worker slot is released either way.
* Search being unavailable is reported as a failure the person can act on, never "completed".
* On a local model (one request at a time) a second run waits for the first instead of both
  crawling and timing out; a waiting run can be cancelled and then never starts.
* A late write from a worker never turns a cancelled job back into a running or completed one.
* The plan preview and the progress stream never block the server.
"""

import asyncio
import tempfile
from contextlib import contextmanager
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import research_fakes as f

from app.core.config import active_profile_database_url
from app.services.disclosure import Labeled, public
from app.services.research import jobs, store
from app.services.research.engine import EngineDependencies
from app.services.research.types import DepthMode, JobStatus


def fake_deps(scenario=f.EIFFEL, profile=f.GPU_DESKTOP, **kwargs):
    clock = f.FakeClock()
    model = f.ScriptedModel(scenario, profile, f.CAREFUL, clock)
    return EngineDependencies(
        llm_factory=model,
        providers=[] if kwargs.get("no_providers") else [
            f.FakeSearch(scenario, down=kwargs.get("search_down", False), clock=clock)
        ],
        fetcher=f.FakeFetcher(scenario, clock=clock),
        capability=profile.capability,
        cache=None,
        clock=clock,
        embed=None,
        max_fetch_workers=2,
        fetch_retries=0,
    )


class Scratch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        token = active_profile_database_url.set(f"sqlite:///{Path(self.temp.name) / 'neo.db'}")
        self.addCleanup(active_profile_database_url.reset, token)
        store.initialize_research_tables()

    def create(self, query=f.EIFFEL.query, depth=DepthMode.QUICK, profile_id=None):
        job = jobs.create_job(Labeled(query, public("user_input:check")), depth,
                              memory_enabled=False, profile_id=profile_id)
        return job.id

    def status(self, job_id):
        return store.load_job(job_id)["status"]


class Lifecycle(Scratch):
    def test_a_run_is_persisted_to_a_terminal_status(self):
        job_id = self.create()
        with patch.object(jobs, "default_dependencies", return_value=fake_deps()), \
                f._machine(f.GPU_DESKTOP, f.GPU_DESKTOP.pace()):
            jobs._run_research_pipeline(job_id, threading.Event())
        data = store.load_job(job_id)
        self.assertEqual(data["status"], JobStatus.COMPLETED.value, data.get("error"))
        self.assertTrue(data["claims"])
        self.assertIn("completion_status", data["metadata"])
        self.assertIn("diagnostics", data["trace"])

    def test_an_engine_crash_is_recorded_and_the_slot_released(self):
        job_id = self.create()
        cancel = threading.Event()
        with jobs._lock:
            jobs._active_jobs[job_id] = cancel
        with patch.object(jobs, "run_research", side_effect=RuntimeError("boom")):
            jobs._run_research_pipeline_bound(job_id, cancel, None, False)
        data = store.load_job(job_id)
        self.assertEqual(data["status"], JobStatus.FAILED.value)
        self.assertNotIn(job_id, jobs.running_job_ids())

    def test_a_profile_that_cannot_open_is_recorded_and_the_slot_released(self):
        job_id = self.create()
        cancel = threading.Event()
        with jobs._lock:
            jobs._active_jobs[job_id] = cancel

        def broken(*args, **kwargs):
            raise OSError("profile database is locked")

        with patch.object(jobs, "profile_database", side_effect=broken):
            jobs._run_research_pipeline_bound(job_id, cancel, "profile-1", False)
        self.assertEqual(self.status(job_id), JobStatus.FAILED.value)
        self.assertNotIn(job_id, jobs.running_job_ids())

    def test_unavailable_search_is_a_failure_not_a_completion(self):
        job_id = self.create()
        with patch.object(jobs, "default_dependencies",
                          return_value=fake_deps(search_down=True)), \
                f._machine(f.GPU_DESKTOP, f.GPU_DESKTOP.pace()):
            jobs._run_research_pipeline(job_id, threading.Event())
        data = store.load_job(job_id)
        self.assertEqual(data["status"], JobStatus.FAILED.value)
        self.assertIn("search", (data.get("error") or "").lower())

    def test_interrupted_jobs_are_recovered(self):
        job_id = self.create()
        store.update_job_status(job_id, JobStatus.SEARCHING.value)
        with patch("app.services.research.store.datetime") as clock:
            from datetime import UTC, datetime, timedelta

            clock.now.return_value = datetime.now(UTC) + timedelta(hours=1)
            clock.fromisoformat = datetime.fromisoformat
            jobs.recover_interrupted_jobs()
        self.assertEqual(self.status(job_id), JobStatus.FAILED.value)


class CancelRaces(Scratch):
    def test_a_late_progress_write_never_overwrites_cancelled(self):
        job_id = self.create()
        store.update_job_status(job_id, JobStatus.CANCELLED.value)
        jobs._update(job_id, JobStatus.SEARCHING, 40, "Reading", "Reading pages")
        store.update_job_status(job_id, JobStatus.SEARCHING.value, current_step="late")
        self.assertEqual(self.status(job_id), JobStatus.CANCELLED.value)

    def test_a_late_result_never_overwrites_cancelled(self):
        job_id = self.create()
        with patch.object(jobs, "default_dependencies", return_value=fake_deps()), \
                f._machine(f.GPU_DESKTOP, f.GPU_DESKTOP.pace()):
            from app.services.research.engine import run_research

            outcome = run_research(Labeled(f.EIFFEL.query, public("user_input:check")),
                                   DepthMode.QUICK, deps=fake_deps())
        store.update_job_status(job_id, JobStatus.CANCELLED.value)
        jobs._save_final(job_id, outcome, [], threading.Event())
        self.assertEqual(self.status(job_id), JobStatus.CANCELLED.value)


class LocalModelQueue(Scratch):
    def start_blocking(self):
        gate = threading.Event()
        started: list[str] = []

        def fake_run(query, depth, **kwargs):
            started.append(kwargs.get("run_id"))
            while not gate.is_set():
                if kwargs["cancelled"]():
                    break
                time.sleep(0.01)
            from app.services.research.engine import run_research

            return run_research(Labeled(f.EIFFEL.query, public("user_input:check")),
                                DepthMode.QUICK, deps=fake_deps(),
                                cancelled=kwargs["cancelled"])

        database = active_profile_database_url.get()

        @contextmanager
        def profile_database(profile_id, guest=False):
            # Worker threads start without the test's context; the app opens the profile there.
            token = active_profile_database_url.set(database)
            try:
                yield
            finally:
                active_profile_database_url.reset(token)

        patches = [
            patch.object(jobs, "profile_database", side_effect=profile_database),
            patch.object(jobs, "run_research", side_effect=fake_run),
            patch.object(jobs, "default_dependencies", return_value=None),
            patch.object(jobs, "model_slot_key", return_value="ollama:local", create=True),
            f._machine(f.GPU_DESKTOP, f.GPU_DESKTOP.pace()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return gate, started

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_second_run_waits_for_the_first(self):
        gate, started = self.start_blocking()
        first, second = self.create(profile_id="p1"), self.create(profile_id="p1")
        self.assertTrue(jobs.start_job(first))
        self.assertTrue(self.wait_for(lambda: first in started))
        self.assertTrue(jobs.start_job(second))
        time.sleep(0.3)
        self.assertNotIn(second, started, "two runs shared a one-request local model")
        self.assertIn("waiting", store.load_job(second)["current_step"].lower())
        gate.set()
        self.assertTrue(self.wait_for(lambda: second in started))
        self.assertTrue(self.wait_for(lambda: not jobs.running_job_ids()))

    def test_a_waiting_run_can_be_cancelled_and_never_starts(self):
        gate, started = self.start_blocking()
        first, second = self.create(profile_id="p1"), self.create(profile_id="p1")
        jobs.start_job(first)
        self.assertTrue(self.wait_for(lambda: first in started))
        jobs.start_job(second)
        time.sleep(0.2)
        self.assertTrue(jobs.cancel_job(second))
        gate.set()
        self.assertTrue(self.wait_for(lambda: not jobs.running_job_ids()))
        self.assertNotIn(second, started)
        self.assertEqual(self.status(second), JobStatus.CANCELLED.value)


class Routes(unittest.TestCase):
    def test_plan_preview_is_bounded(self):
        from fastapi import HTTPException

        from app.api.routes import research

        def slow(*args, **kwargs):
            time.sleep(3)

        with patch.object(research, "plan_preview", side_effect=slow), \
                patch.object(research, "PLAN_PREVIEW_SECONDS", 0.2, create=True):
            started = time.monotonic()
            with self.assertRaises(HTTPException) as refused:
                research.research_plan_preview(research.PlanPreviewRequest(question="q"))
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(refused.exception.status_code, 504)

    def test_a_stopped_preview_stops_on_the_server(self):
        # Seen in the UI check: a preview stopped in the browser kept planning on the server,
        # holding the one-request local model, and the next run waited behind it.
        from fastapi import HTTPException
        from starlette.routing import Match

        from app.api.routes import research

        def plan(question, depth, cancelled):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancelled():
                    from app.services.research.llm_json import ResearchCancelled

                    raise ResearchCancelled("stopped")
                time.sleep(0.01)
            raise AssertionError("the preview was never told to stop")

        outcome = {}

        def preview():
            try:
                research.research_plan_preview(research.PlanPreviewRequest(question="q"))
            except HTTPException as exc:
                outcome["status"] = exc.status_code

        with patch.object(research, "plan_preview", side_effect=plan):
            worker = threading.Thread(target=preview)
            worker.start()
            time.sleep(0.2)
            self.assertEqual(research.cancel_research_plan_preview(), {"cancelled": 1})
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome.get("status"), 409)
        scope = {"type": "http", "method": "DELETE", "path": "/research/plan", "root_path": ""}
        endpoint = next(
            route.endpoint.__name__
            for route in research.router.routes
            if route.matches(scope)[0] == Match.FULL
        )
        self.assertEqual(endpoint, "cancel_research_plan_preview")

    def test_progress_stream_reads_the_database_off_the_event_loop(self):
        from app.api.routes import research
        from app.services.research.types import ResearchJob

        threads = []
        job = ResearchJob(id="j1", user_query="q", status=JobStatus.COMPLETED)

        def get_job(job_id):
            threads.append(threading.get_ident())
            return job

        async def consume():
            loop_thread = threading.get_ident()
            with patch.object(research, "get_job", side_effect=get_job):
                response = await research.research_events("j1")
                async for _ in response.body_iterator:
                    pass
            return loop_thread

        loop_thread = asyncio.run(consume())
        self.assertTrue(threads)
        self.assertTrue(any(ident != loop_thread for ident in threads[1:] or threads),
                        "the stream read SQLite on the event loop")


if __name__ == "__main__":
    unittest.main()
