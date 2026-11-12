"""Research Mode end to end, on every kind of machine.

Every scenario runs through the real engine against simulated machines -- a GPU desktop, this
repository's own CPU-only laptop, a 1B model with a small window, a reasoning model and a
hosted API -- and against a careful model and a sloppy one that behaves the way llama3.2:3b
did in the runs that kept coming back empty.

What a run must do, whatever the machine:

* never fail for a recoverable reason (a weak model, a slow machine);
* report findings that rest on accepted evidence the run actually read, with honest strength;
* cover every core question with a finding or a named gap;
* never repeat a search;
* pursue the same research on a slow machine as on a fast one: same questions, same standards,
  similar source diversity. It may take longer; it must not be shallower.
"""

import unittest

import research_fakes as f

from app.services.research.types import CompletionStatus, DepthMode, Tier

OK_STATUSES = {
    CompletionStatus.COMPLETED,
    CompletionStatus.COMPLETED_WITH_GAPS,
    CompletionStatus.CEILING_REACHED,
}
STRENGTHS = {"verified", "limited", "single_source", "disputed", "one_side_only"}


def core_targets(outcome):
    return [t for t in outcome.plan.top_targets() if t.tier == Tier.CORE]


def family(outcome, target_id):
    return {t.id for t in outcome.plan.targets if t.id == target_id or t.parent_id == target_id}


def covered(outcome, target_id) -> bool:
    """A core question is covered by a finding, by unverified evidence, or by a named gap."""
    ids = family(outcome, target_id)
    if any(claim.question_id in ids for claim in outcome.claims):
        return True
    if any(item.question_id in ids for item in getattr(outcome, "unverified", [])):
        return True
    target = outcome.plan.target(target_id)
    return target is not None and target.text.rstrip("?")[:40] in outcome.report


def found_texts(outcome) -> str:
    texts = [claim.text for claim in outcome.claims]
    texts += [item.excerpt for item in getattr(outcome, "unverified", [])]
    return " ".join(texts)


class EndToEnd(unittest.TestCase):
    maxDiff = None

    def assert_sound(self, result, scenario, *, findings_required=True, verified_markers=False):
        outcome = result.outcome
        detail = f"{scenario.key} on {result.model.profile.name} ({result.model.persona.name})"
        self.assertIn(outcome.completion_status, OK_STATUSES, f"{detail}: {outcome.stop_detail}")
        self.assertEqual(f.claim_problems(outcome), [], detail)
        if findings_required:
            self.assertTrue(outcome.claims, f"{detail}: no findings. errors={outcome.trace.errors}")
        # Every core question has a finding or a named gap.
        for target in core_targets(outcome):
            self.assertTrue(covered(outcome, target.id), f"{detail}: {target.text} vanished")
        # The answer words a correct report must contain.
        text = (
            " ".join(claim.text for claim in outcome.claims)
            if verified_markers
            else found_texts(outcome)
        )
        for target_id, words in scenario.markers.items():
            self.assertTrue(
                any(word.lower() in text.lower() for word in words),
                f"{detail}: nothing about {target_id} ({words}) in: {text[:400]}",
            )
        # Honest, known labels.
        for claim in outcome.claims:
            strength = getattr(claim, "strength", "")
            self.assertIn(strength, STRENGTHS, f"{detail}: {claim.id} has no strength label")
            if strength == "verified":
                self.assertTrue(
                    self.independently_backed(outcome, claim),
                    f"{detail}: {claim.text!r} labelled verified on one unofficial source",
                )
        # No search asked twice.
        self.assertEqual(
            len(result.search.queries), len(set(result.search.queries)),
            f"{detail}: repeated searches {result.search.queries}",
        )
        # A report that cites sources lists them.
        if outcome.claims:
            self.assertIn("## Sources", outcome.report, detail)

    @staticmethod
    def independently_backed(outcome, claim) -> bool:
        evidence = f.accepted_evidence(outcome)
        sources = {s.id: s for s in outcome.sources}
        keys, official = set(), False
        for evidence_id in claim.evidence_ids:
            item = evidence.get(evidence_id)
            source = sources.get(item.source_id) if item else None
            if source is None:
                continue
            keys.add(source.independence_key)
            assessment = source.assessment
            if assessment and assessment.role.value == "issuer_record" and (
                assessment.provenance.value in ("A", "B")
            ):
                official = True
        return len(keys) >= 2 or official

    def test_quick_on_every_machine_with_a_careful_model(self):
        for scenario in f.SCENARIOS:
            for profile in f.PROFILES:
                with self.subTest(scenario=scenario.key, machine=profile.name):
                    result = f.run(scenario, profile, DepthMode.QUICK, f.CAREFUL)
                    self.assert_sound(result, scenario, verified_markers=True)

    def test_quick_with_a_sloppy_small_model(self):
        # The model that produced the empty reports: copies example values, misnumbers
        # sentences, names entities instead of ids, never labels its sources.
        for scenario in f.SCENARIOS:
            for profile in (f.CPU_LAPTOP, f.CPU_TINY):
                with self.subTest(scenario=scenario.key, machine=profile.name):
                    result = f.run(scenario, profile, DepthMode.QUICK, f.SLOPPY)
                    self.assert_sound(result, scenario)

    def test_standard_depth(self):
        for scenario in f.SCENARIOS:
            for profile in (f.GPU_DESKTOP, f.CPU_LAPTOP, f.CLOUD_API):
                with self.subTest(scenario=scenario.key, machine=profile.name):
                    result = f.run(scenario, profile, DepthMode.STANDARD, f.CAREFUL)
                    self.assert_sound(result, scenario, verified_markers=True)

    def test_first_run_on_an_unmeasured_cpu_machine(self):
        # No history with the model yet: the GPU-tuned 10-minute limit must not cut it short.
        for scenario in f.SCENARIOS:
            with self.subTest(scenario=scenario.key):
                result = f.run(scenario, f.CPU_LAPTOP, DepthMode.QUICK, f.CAREFUL, measured=False)
                self.assert_sound(result, scenario, verified_markers=True)
                self.assertNotEqual(
                    result.outcome.completion_status, CompletionStatus.CEILING_REACHED,
                    result.outcome.stop_detail,
                )

    def test_same_research_on_slow_and_fast_machines(self):
        """Equivalent depth: same questions pursued and covered, similar source diversity,
        the same evidence standards. Queries and call order may differ."""
        for scenario in f.SCENARIOS:
            for depth in (DepthMode.QUICK, DepthMode.STANDARD):
                runs = {
                    profile.name: f.run(scenario, profile, depth, f.CAREFUL).outcome
                    for profile in (f.GPU_DESKTOP, f.CPU_LAPTOP, f.CPU_TINY, f.CLOUD_API)
                }
                with self.subTest(scenario=scenario.key, depth=depth.value):
                    reference = runs["gpu_desktop"]
                    pursued = sorted(t.text for t in core_targets(reference))
                    for name, outcome in runs.items():
                        self.assertEqual(sorted(t.text for t in core_targets(outcome)), pursued, name)
                        coverage = {t.text for t in core_targets(outcome) if covered(outcome, t.id)}
                        self.assertEqual(coverage, set(pursued), name)
                        self.assertEqual(
                            outcome.completion_status, reference.completion_status,
                            f"{name}: {outcome.stop_detail}",
                        )
                        hosts = {
                            s.host_group for s in outcome.sources if s.used
                        }
                        reference_hosts = {s.host_group for s in reference.sources if s.used}
                        self.assertGreaterEqual(
                            len(hosts), min(2, len(reference_hosts)), f"{name}: {hosts}"
                        )
                        strengths = sorted(getattr(c, "strength", "") for c in outcome.claims)
                        self.assertTrue(set(strengths) <= STRENGTHS, name)

    def test_judge_calls_fit_the_model(self):
        # Batches are sized with the instructions counted, and never leave the model too
        # little room to answer.
        for profile in (f.CPU_TINY, f.CPU_LAPTOP, f.THINKING_8B):
            with self.subTest(machine=profile.name):
                result = f.run(f.INSTAGRAM_VS_FACEBOOK, profile, DepthMode.QUICK, f.CAREFUL)
                judged = [c for c in result.model.calls if c.stage == "evidence_judgement"]
                self.assertTrue(judged)
                for call in judged:
                    self.assertGreaterEqual(call.num_predict, 300, call)
                    self.assertLessEqual(
                        call.prompt_tokens + call.num_predict, profile.context_window, call
                    )

    def test_structured_output_is_requested(self):
        # Every structured call asks the provider for JSON that fits the stage's schema.
        result = f.run(f.EIFFEL, f.CPU_LAPTOP, DepthMode.QUICK, f.CAREFUL)
        for call in result.model.calls:
            self.assertIsNotNone(call.schema, f"{call.stage} sent without a schema")


if __name__ == "__main__":
    unittest.main()
