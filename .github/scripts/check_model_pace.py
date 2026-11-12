"""Time limits follow the model's measured speed on this machine, never below the defaults.

Neo's limits were constants sized for a GPU: 240 s per model call, 20 minutes per agent run,
30 per standard research run. On a CPU-only machine the same model is ten to twenty times
slower, and those constants failed real work -- research calls timed out at 240 s, research
planning gave up, agent runs ran out of time. These checks hold both halves of the fix: a slow
machine gets the room its measured speed needs, and a fast machine (or a model with no history)
gets exactly the limits it had.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.core.config import active_profile_database_url
from app.services.llm import LLMChatResult, LLMMessage
from app.services.provider_runtime import pace
from app.services.provider_runtime.pace import ModelPace

#: A GPU: what the defaults were written for.
FAST = ModelPace(model="m", samples=20, decode_tps=60.0, prefill_tps=1500.0,
                 load_seconds=1.0, call_seconds=6.0)
#: This CPU-only machine, as its own request log measured qwen3:8b.
SLOW = ModelPace(model="m", samples=20, decode_tps=2.4, prefill_tps=345.0,
                 load_seconds=0.0, call_seconds=118.6)


class Scratch(unittest.TestCase):
    """A profile database of its own, so the real request log is never read."""

    def setUp(self):
        # Windows refuses to delete a database file another module still has open.
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "neo.db"
        token = active_profile_database_url.set(f"sqlite:///{self.db}")
        self.addCleanup(active_profile_database_url.reset, token)
        from app.services.provider_runtime import store

        store.initialize_provider_runtime_tables()
        pace.reset_cache()
        self.addCleanup(pace.reset_cache)

    def log(self, model, route, *, seconds, completion=500, decode_ms=None, status="completed"):
        usage = {"prompt_tokens": 1000, "prefill_ms": 2000, "completion_tokens": completion,
                 "decode_ms": decode_ms if decode_ms is not None else int(seconds * 900),
                 "load_ms": 0}
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO workspace_provider_requests (id, route_name, provider_name,"
                " model_name, request_type, status, latency_ms, provider_usage_json, created_at)"
                " VALUES (lower(hex(randomblob(8))), ?, 'ollama', ?, 'chat', ?, ?, ?,"
                " strftime('%Y-%m-%dT%H:%M:%f', 'now'))",
                (route, model, status, int(seconds * 1000), json.dumps(usage)),
            )
            conn.commit()
        finally:
            conn.close()


class MeasurementChecks(Scratch):
    def test_no_history_is_no_measurement(self):
        self.assertIsNone(pace.measured("qwen3:8b", "research"))
        self.log("qwen3:8b", "research", seconds=100)
        self.log("qwen3:8b", "research", seconds=100)
        pace.reset_cache()
        self.assertIsNone(pace.measured("qwen3:8b", "research"))

    def test_speed_is_read_from_the_providers_own_timings(self):
        for _ in range(5):
            self.log("qwen3:8b", "research", seconds=200, completion=480, decode_ms=200_000)
        measured = pace.measured("qwen3:8b", "research")
        self.assertAlmostEqual(measured.decode_tps, 2.4)
        self.assertAlmostEqual(measured.prefill_tps, 500.0)
        self.assertAlmostEqual(measured.call_seconds, 200.0)

    def test_route_history_preferred_and_failures_ignored(self):
        for _ in range(4):
            self.log("qwen3:8b", "chat", seconds=10)
            self.log("qwen3:8b", "research", seconds=300)
            self.log("qwen3:8b", "research", seconds=999, status="failed")
        self.assertAlmostEqual(pace.measured("qwen3:8b", "research").call_seconds, 300.0)
        self.assertAlmostEqual(pace.measured("qwen3:8b", "chat").call_seconds, 10.0)
        # A route with too little history of its own falls back to the model's overall pace.
        self.assertIsNotNone(pace.measured("qwen3:8b", "agent"))


class LimitChecks(unittest.TestCase):
    def test_call_timeout_unchanged_on_a_fast_machine(self):
        for output in (400, 900, 2048):
            with self.subTest(output=output):
                self.assertEqual(
                    pace.call_timeout(240, FAST, prompt_tokens=1500, max_output_tokens=output), 240
                )

    def test_an_unmeasured_model_gets_time_for_its_first_answer(self):
        # Nothing is known before it answers once: it may be loading a large model onto a CPU.
        self.assertEqual(
            pace.call_timeout(240, None, prompt_tokens=1500, max_output_tokens=8192),
            pace.FIRST_CALL_SECONDS,
        )
        self.assertEqual(
            pace.call_timeout(3000, None, prompt_tokens=1500, max_output_tokens=8192), 3000
        )

    def test_call_timeout_grows_on_a_slow_machine_up_to_the_cap(self):
        research = pace.call_timeout(240, SLOW, prompt_tokens=1300, max_output_tokens=2048)
        self.assertGreater(research, 2048 / SLOW.decode_tps)  # time to write the allowance
        self.assertLessEqual(research, pace.MAX_CALL_SECONDS)
        self.assertEqual(
            pace.call_timeout(240, SLOW, prompt_tokens=1300, max_output_tokens=100_000),
            pace.MAX_CALL_SECONDS,
        )

    def test_run_ceiling_unchanged_on_a_fast_machine(self):
        self.assertEqual(pace.scaled_ceiling(1800, FAST, calls=26), 1800)
        self.assertEqual(pace.scaled_ceiling(1800, None, calls=26), 1800)

    def test_run_ceiling_grows_on_a_slow_machine_up_to_the_cap(self):
        grown = pace.scaled_ceiling(1800, SLOW, calls=26)
        self.assertGreater(grown, 1800)
        self.assertLessEqual(grown, pace.MAX_RUN_SECONDS)
        self.assertEqual(pace.scaled_ceiling(1800, SLOW, calls=10_000), pace.MAX_RUN_SECONDS)

    def test_a_large_model_on_a_weak_machine_is_given_its_whole_run(self):
        # Ten minutes a call: someone chose accuracy over speed, and the mode's work still fits.
        crawling = ModelPace(model="big", samples=10, decode_tps=0.8, prefill_tps=40.0,
                             load_seconds=120.0, call_seconds=400.0, mean_call_seconds=600.0)
        ceiling = pace.scaled_ceiling(1800, crawling, calls=40, extra_seconds=60)
        self.assertGreaterEqual(ceiling, 40 * 600)
        self.assertLessEqual(ceiling, pace.MAX_RUN_SECONDS)

    def test_run_time_is_budgeted_on_the_mean_call(self):
        # Many short calls and a few very long ones: the median would size the run at a fraction.
        mixed = ModelPace(model="m", samples=10, decode_tps=3.0, prefill_tps=300.0,
                          load_seconds=0.0, call_seconds=72.0, mean_call_seconds=300.0)
        self.assertEqual(mixed.budget_seconds(), 300.0)
        self.assertEqual(pace.scaled_ceiling(600, mixed, calls=20), 20 * 300.0 * pace.SAFETY)


class WiringChecks(Scratch):
    def run_request(self, measured, *, provider_timeout=240, caller_timeout=None):
        from app.services.provider_runtime import service

        seen = {}

        class Client:
            def chat_with_metadata(self, messages, **kwargs):
                return LLMChatResult(content="ok", finish_reason="stop")

        def build_client(provider, model, **kwargs):
            seen["timeout"] = kwargs.get("timeout")
            return Client()

        route = {
            "route_name": "research", "provider": "mock", "model": "m", "fallback_chain": [],
            "provider_record": {"provider_type": "mock", "timeout_seconds": provider_timeout},
            "model_record": {"model_name": "m", "context_window": 8192},
            "route_record": {}, "provider_id": "p", "model_id": "m", "reason": "",
        }
        with patch.object(service, "select", return_value=route), \
                patch.object(service, "build_client", side_effect=build_client), \
                patch.object(service.pace, "measured", return_value=measured):
            result = service.ProviderRuntimeService(initialize=False).complete(
                request_type="chat", route_name="research", max_tokens=2048,
                messages=[LLMMessage(role="user", content="hello")], timeout=caller_timeout,
            )
        self.assertEqual(result.status, "completed", result)
        return seen["timeout"]

    def test_fast_machine_keeps_the_provider_timeout(self):
        self.assertEqual(self.run_request(FAST), 240)
        self.assertEqual(self.run_request(None), pace.FIRST_CALL_SECONDS)

    def test_slow_machine_gets_the_time_it_needs(self):
        self.assertGreater(self.run_request(SLOW), 900)

    def test_a_callers_longer_timeout_is_honoured_never_a_shorter_one(self):
        self.assertEqual(self.run_request(FAST, caller_timeout=900), 900)
        self.assertEqual(self.run_request(FAST, caller_timeout=60), 240)

    def test_research_limit_follows_the_machine(self):
        from app.services.research.capability import ModelCapability
        from app.services.research.engine import EngineDependencies, _Run
        from app.services.research.types import DepthMode

        def make_run(measured):
            deps = EngineDependencies(
                llm_factory=lambda n: None, providers=[], fetcher=None,
                capability=lambda: ModelCapability(model="qwen3:8b", max_output_tokens=2048,
                                                   context_window=40960, known=True),
                embed=None,
            )
            with patch.object(pace, "measured", return_value=measured):
                return _Run("q", DepthMode.STANDARD, deps, None, lambda: False, "", "run")

        fast = make_run(FAST)
        self.assertEqual(fast.ceiling_seconds, 1800)
        self.assertEqual(fast.deadline - fast.started, 1800)
        slow = make_run(SLOW)
        self.assertGreater(slow.ceiling_seconds, 1800)
        self.assertAlmostEqual(slow.deadline - slow.started, slow.ceiling_seconds, places=3)
        self.assertTrue(any("time limit was raised" in note for note in slow.trace.errors))


class AgentLimitChecks(unittest.TestCase):
    def test_agent_limit_follows_the_machine(self):
        from app.services.agent_core import service

        with patch("app.services.provider_runtime.router.select",
                   return_value={"model": "llama3.2:3b"}):
            for measured, expect_default in ((FAST, True), (None, True), (SLOW, False)):
                with self.subTest(measured=measured), \
                        patch.object(pace, "measured", return_value=measured):
                    seconds = service._own_loop_budgets().max_seconds
                    self.assertEqual(seconds == 1200, expect_default, seconds)
        with patch("app.services.provider_runtime.router.select", side_effect=RuntimeError):
            self.assertEqual(service._own_loop_budgets().max_seconds, 1200)


class EstimateChecks(unittest.TestCase):
    """The research time shown to the user follows the machine, the mode and the plan."""

    def estimates(self, measured):
        from app.services.research import engine
        from app.services.research.capability import ModelCapability

        capability = ModelCapability(model="qwen3:8b", max_output_tokens=2048,
                                     context_window=40960, known=True)
        with patch.object(engine, "resolve_capability", return_value=capability), \
                patch.object(pace, "measured", return_value=measured):
            return engine.time_estimates()["modes"]

    def test_unmeasured_model_gives_no_number(self):
        for mode in self.estimates(None).values():
            self.assertIsNone(mode["seconds"])

    def test_estimate_follows_mode_and_machine(self):
        fast, slow = self.estimates(FAST), self.estimates(SLOW)
        for modes in (fast, slow):
            self.assertLessEqual(modes["quick"]["seconds"], modes["standard"]["seconds"])
            self.assertLessEqual(modes["standard"]["seconds"], modes["deep"]["seconds"])
            for mode in modes.values():
                self.assertLessEqual(mode["seconds"], mode["ceiling_seconds"])
        # A GPU finishes standard research well inside its 30 minutes; this CPU does not.
        self.assertLess(fast["standard"]["seconds"], 15 * 60)
        self.assertGreater(slow["standard"]["seconds"], 30 * 60)
        for depth in fast:
            self.assertGreater(slow[depth]["seconds"], fast[depth]["seconds"])

    def test_remaining_time_counts_down_with_the_work(self):
        from app.services.research import estimate
        from app.services.research.policy import POLICIES
        from app.services.research.types import DepthMode

        run = estimate.shape(POLICIES[DepthMode.STANDARD], planning_calls=6, judge_calls=4,
                             finishing_calls=16, call_limit=72, targets=6)

        def left(**progress):
            values = dict(calls_used=0, steps_done=0, planned=False, finishing_calls_used=None,
                          run_seconds_per_call=0.0, run_step_seconds=None,
                          prior_seconds_per_call=100.0, seconds_left=1e9)
            values.update(progress)
            return estimate.remaining(run, **values)

        start = left()
        planned = left(calls_used=6, planned=True, run_seconds_per_call=100.0)
        researched = left(calls_used=18, planned=True, steps_done=2, run_seconds_per_call=100.0)
        finishing = left(calls_used=30, planned=True, steps_done=3, finishing_calls_used=8,
                         run_seconds_per_call=100.0)
        self.assertGreater(start, planned)
        self.assertGreater(planned, researched)
        self.assertGreater(researched, finishing)
        self.assertEqual(finishing, 8 * 100)  # only the finishing calls still to come
        # The run's own pace takes over from the machine's history once it has made calls:
        # 34 calls to go (3 steps of 6, then 16 finishing) at 10 s, plus 3 steps of reading.
        searching = 3 * estimate.STEP_SECONDS
        self.assertEqual(planned, 34 * 100 + searching)
        self.assertEqual(left(calls_used=6, planned=True, run_seconds_per_call=10.0),
                         34 * 10 + searching)
        # Never more than the time the run has left, and nothing to go on means no number.
        self.assertEqual(left(seconds_left=120), 120)
        self.assertIsNone(left(prior_seconds_per_call=None))

    def test_running_job_reports_its_estimate(self):
        from app.services.research.capability import ModelCapability
        from app.services.research.engine import EngineDependencies, _Run
        from app.services.research.types import DepthMode

        updates = []
        deps = EngineDependencies(
            llm_factory=lambda n: None, providers=[], fetcher=None, embed=None,
            capability=lambda: ModelCapability(model="qwen3:8b", max_output_tokens=2048,
                                               context_window=40960, known=True),
        )
        for measured in (SLOW, None):
            with self.subTest(measured=measured), patch.object(pace, "measured",
                                                               return_value=measured):
                run = _Run("q", DepthMode.STANDARD, deps, updates.append, lambda: False, "", "r")
                run._emit("planning", 4, "Planning", "Working out what needs to be established")
                remaining = updates[-1].remaining_seconds
                if measured is None:
                    self.assertIsNone(remaining)
                else:
                    self.assertGreater(remaining, 30 * 60)
                    self.assertLessEqual(remaining, run.ceiling_seconds)


class FinishingChecks(unittest.TestCase):
    """A run that found evidence always gets the time to turn it into a report."""

    def make_run(self, clock):
        from app.services.research.capability import ModelCapability
        from app.services.research.engine import EngineDependencies, _Run
        from app.services.research.types import DepthMode

        deps = EngineDependencies(
            llm_factory=lambda n: None, providers=[], fetcher=None, embed=None, clock=clock,
            capability=lambda: ModelCapability(model="qwen3:8b", max_output_tokens=2048,
                                               context_window=40960, known=True),
        )
        with patch.object(pace, "measured", return_value=SLOW):
            return _Run("q", DepthMode.STANDARD, deps, None, lambda: False, "", "r")

    def test_findings_at_the_deadline_still_get_written_up(self):
        now = [0.0]
        run = self.make_run(lambda: now[0])
        run.llm_budget.profile.record("evidence_judgement", seconds=380.0)
        run.llm_budget.profile.record("research_task", seconds=60.0)
        now[0] = run.deadline + 10  # judging ran past the deadline
        run.evidence = [SimpleNamespace(status="accepted"), SimpleNamespace(status="rejected")]
        run._secure_finishing()
        # Room for claim synthesis and the summary at the run's slowest pace.
        self.assertGreaterEqual(run.deadline - now[0], 2 * 380.0)
        self.assertEqual(run.llm_budget.deadline, run.deadline)
        self.assertTrue(run.llm_budget.can_spend(reserved=True))
        self.assertTrue(any("written up" in note for note in run.trace.errors))

    def test_nothing_found_means_no_extension(self):
        now = [0.0]
        run = self.make_run(lambda: now[0])
        run.llm_budget.profile.record("evidence_judgement", seconds=380.0)
        deadline = run.deadline
        now[0] = deadline + 10
        run.evidence = [SimpleNamespace(status="rejected")]
        run._secure_finishing()
        self.assertEqual(run.deadline, deadline)


class StreamingChecks(unittest.TestCase):
    """Local model calls stream, so a slow model that is still writing is never cut off."""

    class Reply:
        def __init__(self, chunks, status_error=None):
            self.lines = [json.dumps(chunk).encode() for chunk in chunks]
            self.status_error = status_error
            self.closed = False

        def raise_for_status(self):
            if self.status_error:
                raise self.status_error

        def iter_lines(self):
            yield from self.lines

        def close(self):
            self.closed = True

    def test_a_streamed_reply_reads_like_a_whole_one(self):
        from app.services.llm import OllamaClient

        reply = self.Reply([
            {"message": {"role": "assistant", "thinking": "Let me "}},
            {"message": {"role": "assistant", "thinking": "think."}},
            {"message": {"role": "assistant", "content": "Hello "}},
            {"message": {"role": "assistant", "content": "there",
                         "tool_calls": [{"function": {"name": "list_dir",
                                                      "arguments": {"path": "."}}}]}},
            {"done": True, "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 5,
             "total_duration": 3_000_000_000, "load_duration": 1_000_000_000,
             "prompt_eval_duration": 500_000_000, "eval_duration": 1_500_000_000,
             "message": {"role": "assistant", "content": ""}},
        ])
        client = OllamaClient("m", "http://127.0.0.1:11434", 240, 100)
        with patch("app.services.llm.requests.post", return_value=reply) as post, \
                patch.object(OllamaClient, "supports_thinking", return_value=False):
            result = client.chat_with_metadata([LLMMessage(role="user", content="hi")],
                                               tools=[{"type": "function"}])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertEqual(result.content, "Hello there")
        self.assertEqual(result.thinking, "Let me think.")
        self.assertEqual([call["name"] for call in result.tool_calls], ["list_dir"])
        self.assertEqual((result.prompt_tokens, result.completion_tokens), (12, 5))
        self.assertEqual((result.load_ms, result.prefill_ms, result.decode_ms), (1000, 500, 1500))
        self.assertEqual(result.finish_reason, "stop")
        self.assertTrue(reply.closed)

    def test_failures_still_fail(self):
        import requests

        from app.services.llm import _collect_ollama_stream

        with self.assertRaises(requests.HTTPError):
            _collect_ollama_stream(self.Reply([{"error": "model failed to load"}]))
        with self.assertRaises(requests.ConnectionError):
            _collect_ollama_stream(self.Reply([{"message": {"content": "cut off"}}]))
        broken = self.Reply([], status_error=requests.HTTPError("500"))
        with self.assertRaises(requests.HTTPError):
            _collect_ollama_stream(broken)
        self.assertTrue(broken.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
