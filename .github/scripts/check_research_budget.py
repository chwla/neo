"""Research planning on a slow, CPU-only machine.

The finishing reserve is projected from the calls a run has made so far. On a machine where
the first planning call took 90 s -- loading the model included -- that projection passed the
run's 30-minute deadline after one call, planning skipped its remaining stages, and the run
failed with "no usable core research targets". A fast machine never got near the limit, which
is why it only appeared on Windows without a GPU.
"""

import json
import unittest
from types import SimpleNamespace

from app.services.research.capability import ModelCapability, work_scale
from app.services.research.engine import _JUDGE_BATCH, JUDGE_OUTPUT_TOKENS_PER_CHUNK
from app.services.research.llm_json import LLMBudget, StageProfile
from app.services.research.planner import plan_research
from app.services.research.policy import POLICIES
from app.services.research.report import REPORT_DEPTH
from app.services.research.types import DepthMode, Tier

QUERY = "research on the history of stark family in game of thrones"
ANSWERS = {
    "research_task": {
        "task_type": "historical_research", "answer_shape": "a history",
        "time_sensitivity": "low", "contested_likely": False,
        "entities": [{"id": "e1", "name": "House Stark", "type": "fictional family",
                      "aliases": ["Starks"]}],
        "constraints": [], "decision_criteria": [],
    },
    "research_dimensions": {
        "dimensions": [{"id": "m1", "name": "History of House Stark",
                        "description": "How the family began and what happened to it",
                        "justification_refs": ["query"]}],
    },
    "research_targets": {
        "targets": [{
            "id": "t1", "text": "What is the history of House Stark in Game of Thrones?",
            "dimension_id": "m1", "tier": "core", "form": "single", "subject_ids": ["e1"],
            "authority_kind": "consensus", "claim_scope": "general", "origin_hint": "unknown",
            "record_form": "other", "recency": "timeless", "error_prone": "none",
            "hypotheses": {"likely_issuers": [], "record_title_words": [],
                           "expected_answer": ""},
        }],
    },
}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowModel:
    """Answers each planning stage after ``seconds``; the first call also loads the model."""

    def __init__(self, clock, seconds, load_seconds=0.0):
        self.clock, self.seconds, self.load_seconds = clock, seconds, load_seconds
        self.stages = []

    def __call__(self, num_predict):
        return self

    def chat_with_metadata(self, messages, temperature, num_predict):
        stage = messages[0].content.split("\n", 1)[0].removeprefix("Stage: ")
        load = self.load_seconds if not self.stages else 0.0
        self.stages.append(stage)
        self.clock.now += self.seconds + load
        return SimpleNamespace(
            content=json.dumps(ANSWERS[stage]), finish_reason="stop", prompt_tokens=382,
            completion_tokens=129, load_ms=int(load * 1000) if load else None,
        )


def standard_budget(clock):
    """The budget a standard run gives qwen3:8b (2048-token answers), as the engine builds it."""
    depth = DepthMode.STANDARD
    policy = POLICIES[depth]
    capability = ModelCapability(provider="ollama", model="qwen3:8b", context_window=40960,
                                 max_output_tokens=2048, known=True)
    scale = work_scale(capability, reference_items=_JUDGE_BATCH,
                       output_per_item=JUDGE_OUTPUT_TOKENS_PER_CHUNK)
    reserved = 4 + (policy.entailment != "core_decisive") + policy.union_check
    reserved += REPORT_DEPTH[depth].writing_reserve
    return LLMBudget(policy.ceilings.model_calls * scale, reserved * scale,
                     capability=capability, profile=StageProfile(clock), clock=clock,
                     deadline=clock() + policy.ceilings.seconds)


class ResearchBudgetChecks(unittest.TestCase):
    def plan(self, seconds, load_seconds=0.0):
        clock = Clock()
        model = SlowModel(clock, seconds, load_seconds)
        plan = plan_research(QUERY, DepthMode.STANDARD, llm_factory=model,
                             budget=standard_budget(clock))
        return plan, model

    def test_slow_first_call_still_plans(self):
        # The failed run: one 90.6 s call, no load time reported.
        plan, model = self.plan(90.571)
        self.assertEqual(model.stages,
                         ["research_task", "research_dimensions", "research_targets"])
        self.assertTrue(any(t.tier == Tier.CORE for t in plan.top_targets()))

    def test_fast_machine_plans_exactly_as_before(self):
        plan, model = self.plan(5.0)
        self.assertEqual(model.stages,
                         ["research_task", "research_dimensions", "research_targets"])
        self.assertTrue(any(t.tier == Tier.CORE for t in plan.top_targets()))

    def test_model_load_is_not_charged_to_every_later_call(self):
        clock = Clock()
        budget = standard_budget(clock)
        budget.spend()
        budget.profile.record("research_task", seconds=90.0, load_seconds=60.0)
        clock.now = 92.0
        self.assertAlmostEqual(budget.stage_seconds("claim_synthesis"), 30.0)
        self.assertAlmostEqual(budget.seconds_per_call(), 30.0)
        self.assertTrue(budget.can_spend())
        # The trace still shows the time the user actually waited.
        self.assertEqual(budget.profile.as_list()[0]["seconds"], 90.0)

    def test_questions_naming_an_undeclared_aspect_are_kept(self):
        # llama3.2:3b's plan for "what changes in GTA 6 on PS5": every target named an aspect
        # the plan never declared, the planner dropped them all, and the run failed unsearched.
        from app.services.research.planner import PlanDraft, validate_plan

        query = "research between gta5 and gta 6. what changes can i expect playing on ps5?"
        draft = PlanDraft.model_validate({
            "task_type": "comparison",
            "entities": [{"id": "e1", "name": "Grand Theft Auto V"},
                         {"id": "e2", "name": "Grand Theft Auto VI"}],
            "dimensions": [],
            "targets": [
                {"id": "t1", "text": "What changes can I expect playing Grand Theft Auto V on PS5?",
                 "dimension_id": "performance", "tier": "core", "subject_ids": ["e1"]},
                {"id": "t2",
                 "text": "What changes can I expect playing Grand Theft Auto VI on PS5?",
                 "dimension_id": "changes", "tier": "core", "subject_ids": ["e2"]},
                {"id": "t3", "text": "How does PS5 hardware affect Grand Theft Auto VI graphics?",
                 "dimension_id": "", "tier": "supporting", "subject_ids": ["e2"]},
            ],
        })
        plan = validate_plan(draft, query, DepthMode.QUICK)
        top = plan.top_targets()
        # The GTA V and GTA VI versions of one question merge into one asked of each game.
        self.assertEqual(len(top), 2)
        self.assertTrue(any(t.tier == Tier.CORE for t in top))
        self.assertEqual({e for t in top for e in t.subject_ids}, {"e1", "e2"})
        self.assertLessEqual({"Performance", "Answer"},
                             {d.name for d in plan.decomposition.dimensions})
        self.assertFalse([i for i in plan.issues if i.code == "target_without_dimension"])

    def test_reserve_still_applies_after_the_minimum_plan(self):
        # Only the smallest usable plan may use the finishing reserve. Ordinary calls are
        # still refused once the reserve no longer fits, as before.
        clock = Clock()
        budget = standard_budget(clock)
        budget.spend()
        budget.profile.record("research_task", seconds=90.571)
        clock.now = 92.0
        self.assertFalse(budget.can_spend())
        self.assertTrue(budget.can_spend(reserved=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
