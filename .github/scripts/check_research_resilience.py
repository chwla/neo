"""Research Mode under failure: recover when it can, stop fast and honestly when it cannot.

* Recoverable: a slow or timed-out call, invalid JSON, a useless plan, a failed judge batch,
  blocked pages. The run carries on and keeps what it has.
* Unrecoverable: the model is unreachable, search is unavailable, the user cancels. The run
  stops quickly -- before minutes of work where it can -- with a status that says why.
* At the time limit the run stops acquiring evidence, keeps what it accepted, finishes with
  deterministic fallbacks and says exactly where it stopped. It never invents findings.
* Cancelling stops the run wherever it is, and nothing runs after it.
"""

import unittest

import requests
import research_fakes as f

from app.services.research.types import CompletionStatus, DepthMode

RECOVERED = {CompletionStatus.COMPLETED, CompletionStatus.COMPLETED_WITH_GAPS}


class Recoverable(unittest.TestCase):
    def assert_recovered(self, result, *, need_claims=True):
        outcome = result.outcome
        self.assertIn(outcome.completion_status, RECOVERED, outcome.stop_detail)
        self.assertEqual(f.claim_problems(outcome), [])
        if need_claims:
            self.assertTrue(outcome.claims, f"no findings; errors={outcome.trace.errors}")

    def test_useless_plan_falls_back_to_a_plan_from_the_query(self):
        garbage = {
            "research_plan": "I am not able to make a plan for this request.",
            "research_plan_repair": "Sorry.",
            "research_task": "Sorry.",
        }
        for scenario in (f.INSTAGRAM_VS_FACEBOOK, f.EIFFEL):
            with self.subTest(scenario=scenario.key):
                result = f.run(scenario, f.CPU_LAPTOP, model_kwargs={"garbage_stages": garbage})
                self.assert_recovered(result)
                codes = {issue.code for issue in result.outcome.plan.issues}
                self.assertIn("fallback_plan", codes)

    def test_plan_with_no_targets_falls_back(self):
        garbage = {"research_plan": '{"task_type": "comparison", "targets": []}',
                   "research_plan_repair": '{"targets": []}'}
        result = f.run(f.INSTAGRAM_VS_FACEBOOK, f.GPU_DESKTOP, model_kwargs={"garbage_stages": garbage})
        self.assert_recovered(result)

    def test_first_call_timing_out_while_the_model_loads(self):
        result = f.run(
            f.EIFFEL, f.CPU_LAPTOP,
            model_kwargs={"fail_calls": {0: requests.Timeout("Read timed out")}},
        )
        self.assert_recovered(result)

    def test_timeouts_mid_run_degrade_instead_of_failing(self):
        for stage in ("research_queries", "evidence_judgement", "claim_synthesis",
                      "entailment_audit", "report_summary"):
            with self.subTest(stage=stage):
                result = f.run(
                    f.INSTAGRAM_VS_FACEBOOK, f.CPU_LAPTOP,
                    model_kwargs={"fail_stages": {stage: requests.Timeout("Read timed out")}},
                )
                self.assert_recovered(result)
                self.assertTrue(result.outcome.report.strip())

    def test_malformed_and_cut_off_answers(self):
        result = f.run(f.INSTAGRAM_VS_FACEBOOK, f.CPU_LAPTOP, persona=f.BROKEN_JSON)
        self.assert_recovered(result)

    def test_empty_claims_answer_gets_extractive_findings_that_are_still_checked(self):
        result = f.run(
            f.EIFFEL, f.CPU_LAPTOP,
            model_kwargs={"garbage_stages": {"claim_synthesis": '{"claims": []}'}},
        )
        self.assert_recovered(result)
        evidence = f.accepted_evidence(result.outcome)
        for claim in result.outcome.claims:
            excerpts = " ".join(evidence[e].excerpt for e in claim.evidence_ids)
            self.assertTrue(claim.text[:30].lower() in excerpts.lower() or claim.strength,
                            claim.text)

    def test_all_pages_refusing_access_is_an_honest_gap(self):
        result = f.run(f.INSTAGRAM_VS_FACEBOOK, f.GPU_DESKTOP, all_blocked=True)
        outcome = result.outcome
        self.assertNotEqual(outcome.completion_status, CompletionStatus.COMPLETED)
        self.assertNotEqual(outcome.completion_status, CompletionStatus.FAILED)
        self.assertEqual(outcome.claims, [])
        self.assertIn("refused", outcome.report.lower())


class Unrecoverable(unittest.TestCase):
    def test_unreachable_model_fails_fast_before_searching(self):
        result = f.run(f.EIFFEL, f.CPU_LAPTOP, model_kwargs={"unreachable": True})
        outcome = result.outcome
        self.assertEqual(outcome.completion_status, CompletionStatus.FAILED)
        self.assertEqual(result.search.queries, [], "searched with no model to read the results")
        self.assertLess(result.seconds, 120)
        detail = outcome.stop_detail.lower()
        self.assertTrue("reach" in detail or "unavailable" in detail, detail)

    def test_search_down_is_blocked_before_planning(self):
        result = f.run(f.EIFFEL, f.CPU_LAPTOP, search_down=True)
        outcome = result.outcome
        self.assertEqual(outcome.completion_status, CompletionStatus.BLOCKED)
        self.assertNotIn("research_plan", result.model.stages())
        self.assertLess(result.seconds, 120)
        self.assertIn("search", outcome.stop_detail.lower())

    def test_no_search_provider_is_blocked_immediately(self):
        result = f.run(f.EIFFEL, f.CPU_LAPTOP, no_providers=True)
        self.assertEqual(result.outcome.completion_status, CompletionStatus.BLOCKED)
        self.assertEqual(result.model.stages(), [])


class Deadline(unittest.TestCase):
    """The time limit reached in each stage: stop acquiring, keep, finish, say where."""

    STAGES = {
        "planning": "research_plan",
        "judging": "evidence_judgement",
        "claims": "claim_synthesis",
        "verification": "entailment_audit",
    }

    #: A writer that paraphrases, so its claims go to the model audit (verbatim ones do not).
    PARAPHRASER = f.Persona("paraphraser", paraphrase=True)

    def run_late(self, stage, delay=200_000):
        return f.run(
            f.INSTAGRAM_VS_FACEBOOK, f.GPU_DESKTOP, persona=self.PARAPHRASER,
            model_kwargs={"stage_delay": {stage: delay}},
        )

    def judging_delay(self):
        # A judge batch that finishes past the research limit but inside the hard limit: what
        # it accepted must be kept. One that would end past the hard limit is stopped there and
        # accepts nothing (test_a_call_writing_at_the_hard_limit_is_stopped).
        result = self.run_late("evidence_judgement", 0)
        timing = result.outcome.trace.diagnostics["timing"]
        first = next(c for c in result.model.calls if c.stage == "evidence_judgement")
        ceiling, hard = timing["ceiling_seconds"], timing["hard_limit_seconds"]
        return ceiling + (hard - ceiling) / 2 - (first.started - result.started)

    def test_deadline_in_each_stage(self):
        judging = self.judging_delay()
        for where, stage in self.STAGES.items():
            with self.subTest(stage=where):
                result = self.run_late(stage, judging if where == "judging" else 200_000)
                outcome = result.outcome
                timing = (getattr(outcome.trace, "diagnostics", {}) or {}).get("timing", {})
                self.assertIn("hard_limit_seconds", timing, "the run must record its hard limit")
                hard = result.started + timing["hard_limit_seconds"]
                late = [c.stage for c in result.model.calls if c.started > hard]
                self.assertEqual(late, [], f"model calls started after the hard limit: {late}")
                # A call already writing when the limit came is stopped there, not left to run
                # on (seen live: one CPU claims call ran 18 minutes past where it should end).
                self.assertLessEqual(
                    result.seconds, timing["hard_limit_seconds"] + 60,
                    "a model call under way ran on past the hard limit",
                )
                self.assertEqual(outcome.completion_status, CompletionStatus.CEILING_REACHED)
                self.assertIn("time", outcome.stop_detail.lower())
                self.assertEqual(timing.get("deadline_hit"), where)
                self.assertEqual(f.claim_problems(outcome), [])
                searches_after = [
                    a for a in outcome.trace.search_attempts
                    if a.round > 1 and where in ("planning", "judging")
                ]
                self.assertEqual(searches_after, [], "kept searching after the time limit")
                if where != "planning":
                    self.assertTrue(f.accepted_evidence(outcome), "accepted evidence was lost")
                if where in ("claims", "verification"):
                    self.assertTrue(
                        outcome.claims or getattr(outcome, "unverified", []),
                        "findings already in hand were not reported",
                    )
                self.assertTrue(outcome.report.strip())

    def test_a_call_writing_at_the_hard_limit_is_stopped(self):
        result = self.run_late("evidence_judgement")
        outcome = result.outcome
        timing = outcome.trace.diagnostics["timing"]
        self.assertLessEqual(result.seconds, timing["hard_limit_seconds"] + 60)
        stopped = [c for c in result.model.calls if c.failed == "stopped"]
        self.assertEqual([c.stage for c in stopped], ["evidence_judgement"])
        self.assertEqual(outcome.completion_status, CompletionStatus.CEILING_REACHED)
        self.assertEqual(f.claim_problems(outcome), [])
        self.assertTrue(outcome.report.strip())


class Cancellation(unittest.TestCase):
    def cancel_when(self, hook_name, predicate):
        state = {"cancelled": False, "calls": None, "fetches": None}

        def cancelled():
            return state["cancelled"]

        def on_call(model, record):
            if hook_name == "call" and not state["cancelled"] and predicate(record):
                state["cancelled"] = True
                state["calls"] = len(model.calls)

        def on_fetch(fetcher, url):
            if hook_name == "fetch" and not state["cancelled"]:
                state["cancelled"] = True

        def on_search(search, query):
            if hook_name == "search" and not state["cancelled"]:
                state["cancelled"] = True

        result = f.run(
            f.INSTAGRAM_VS_FACEBOOK, f.CPU_LAPTOP,
            cancelled=cancelled, model_kwargs={"on_call": on_call},
            on_fetch=on_fetch, on_search=on_search,
        )
        return result, state

    def test_cancel_during_generation(self):
        for stage in ("research_plan", "evidence_judgement", "claim_synthesis"):
            with self.subTest(stage=stage):
                result, state = self.cancel_when("call", lambda r, s=stage: r.stage == s)
                self.assertEqual(result.outcome.completion_status, CompletionStatus.CANCELLED)
                self.assertEqual(len(result.model.calls), state["calls"],
                                 "a model call started after cancelling")

    def test_cancel_during_search(self):
        result, _ = self.cancel_when("search", None)
        self.assertEqual(result.outcome.completion_status, CompletionStatus.CANCELLED)
        self.assertEqual(result.fetcher.fetched, [], "pages were read after cancelling")
        self.assertNotIn("evidence_judgement", result.model.stages())

    def test_cancel_during_fetch(self):
        result, _ = self.cancel_when("fetch", None)
        self.assertEqual(result.outcome.completion_status, CompletionStatus.CANCELLED)
        self.assertNotIn("evidence_judgement", result.model.stages())


if __name__ == "__main__":
    unittest.main()
