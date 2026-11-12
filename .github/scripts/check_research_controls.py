"""Research history and plan controls: delete one run, clear all, and run an edited plan.

* Deleting a run, or clearing history, stops a run still in progress first: otherwise its worker
  carries on invisibly, holding the model, and its next save writes the row back.
* ``DELETE /research/clear`` clears; it is never read as deleting a job called "clear".
* A plan edited in the preview is checked by the planner's own rules before it runs, and the
  run researches it instead of planning its own.
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import active_profile_database_url
from app.services.research import jobs, store
from app.services.research.planner import PlanDraft, PlanningFailed, plan_from_edit, validate_plan
from app.services.research.types import DepthMode, Tier

QUERY = "research on the history of stark family in game of thrones"


class Scratch(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        token = active_profile_database_url.set(f"sqlite:///{Path(self.temp.name) / 'neo.db'}")
        self.addCleanup(active_profile_database_url.reset, token)
        store.initialize_research_tables()

    def job(self, job_id, status="completed"):
        store.save_job({
            "id": job_id, "user_query": f"question {job_id}", "depth": "standard",
            "status": status, "created_at": "2026-11-11T00:00:00+00:00",
            "updated_at": "2026-11-11T00:00:00+00:00", "metadata": {},
        })

    def ids(self):
        return sorted(job["id"] for job in store.list_jobs(limit=50))


class HistoryChecks(Scratch):
    def test_delete_one_run(self):
        self.job("a")
        self.job("b")
        self.assertTrue(jobs.delete_job("a"))
        self.assertEqual(self.ids(), ["b"])
        self.assertFalse(jobs.delete_job("a"))

    def test_clear_all(self):
        for job_id in ("a", "b", "c"):
            self.job(job_id)
        self.assertEqual(jobs.clear_jobs(), 3)
        self.assertEqual(self.ids(), [])

    def test_deleting_a_running_job_stops_it(self):
        self.job("live", status="searching")
        cancel = threading.Event()
        with jobs._lock:
            jobs._active_jobs["live"] = cancel
        self.addCleanup(jobs._active_jobs.pop, "live", None)
        self.assertTrue(jobs.delete_job("live"))
        self.assertTrue(cancel.is_set())
        self.assertEqual(self.ids(), [])

    def test_clear_stops_every_running_job(self):
        self.job("live", status="searching")
        self.job("done")
        cancel = threading.Event()
        with jobs._lock:
            jobs._active_jobs["live"] = cancel
        self.addCleanup(jobs._active_jobs.pop, "live", None)
        self.assertEqual(jobs.clear_jobs(), 2)
        self.assertTrue(cancel.is_set())

    def test_a_deleted_run_is_not_written_back(self):
        # The worker read the job just before it was deleted; once cancelled it must not save.
        self.job("racing", status="queued")
        loaded = store.load_job("racing")
        store.delete_job("racing")
        cancel = threading.Event()
        cancel.set()
        with patch.object(jobs, "load_job", return_value=loaded), \
                patch.object(jobs, "_update"), patch.object(jobs, "run_research") as run:
            jobs._run_research_pipeline("racing", cancel)
        run.assert_not_called()
        self.assertEqual(self.ids(), [])


class RouteChecks(unittest.TestCase):
    @staticmethod
    def handler(method, path):
        """The endpoint a request reaches: the first route that fully matches, as served."""
        from starlette.routing import Match

        from app.api.routes import research

        scope = {"type": "http", "method": method, "path": path, "root_path": ""}
        # In declaration order, which is the order the server tries them in.
        for route in research.router.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return route.endpoint.__name__
        return None

    def test_fixed_paths_are_never_read_as_a_job_id(self):
        self.assertEqual(self.handler("DELETE", "/research/clear"), "clear_research_jobs")
        self.assertEqual(self.handler("DELETE", "/research/abc123"), "delete_research_job")
        self.assertEqual(self.handler("GET", "/research/estimates"), "research_time_estimates")
        self.assertEqual(self.handler("GET", "/research/abc123"), "get_research_job")

    def test_delete_and_clear_endpoints(self):
        from fastapi import HTTPException

        from app.api.routes import research

        with patch.object(research, "clear_jobs", return_value=2):
            self.assertEqual(research.clear_research_jobs(), {"cleared": 2})
        with patch.object(research, "delete_job", return_value=True) as delete:
            self.assertEqual(research.delete_research_job("abc123"), {"deleted": "abc123"})
        delete.assert_called_once_with("abc123")
        with patch.object(research, "delete_job", return_value=False), \
                self.assertRaises(HTTPException) as missing:
            research.delete_research_job("missing")
        self.assertEqual(missing.exception.status_code, 404)

    def test_an_unusable_edited_plan_is_refused_before_anything_runs(self):
        from fastapi import HTTPException

        from app.api.routes import research
        from app.services.research.types import StartResearchRequest

        request = StartResearchRequest(
            query=QUERY, memory_enabled=False,
            plan={"decomposition": {"dimensions": []}, "targets": []},
        )
        with patch.object(research, "session_for", return_value=None), \
                patch.object(research, "create_job") as create, \
                self.assertRaises(HTTPException) as refused:
            research.start_research(request, None)
        self.assertEqual(refused.exception.status_code, 422)
        create.assert_not_called()


class SearchSetupChecks(unittest.TestCase):
    """A fresh local install can search, and research says so plainly when it cannot."""

    def test_fresh_install_searches_like_the_docker_image(self):
        import os

        from app.core.config import Settings
        from app.services.search.providers import ProviderRegistry

        keys = [key for key in os.environ if "SEARCH" in key.upper()]
        with patch.dict(os.environ, {}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            settings = Settings(_env_file=None)
            self.assertTrue(settings.web_search_enabled)
            self.assertEqual(settings.web_search_provider, "searxng")
            registry = ProviderRegistry()
            registry.settings = settings
            self.assertEqual([p.name for p in registry.chain()],
                             ["searxng", "duckduckgo", "bing_html", "wikipedia"])

    def test_research_is_refused_up_front_when_search_is_off(self):
        from fastapi import HTTPException

        from app.api.routes import research
        from app.services.research.types import StartResearchRequest
        from app.services.search.providers import DisabledSearchProvider, ProviderRegistry

        with patch.object(ProviderRegistry, "chain", return_value=[DisabledSearchProvider()]):
            self.assertFalse(research._search_available())
            with patch.object(research, "create_job") as create, \
                    self.assertRaises(HTTPException) as refused:
                research.start_research(StartResearchRequest(query=QUERY), None)
        self.assertEqual(refused.exception.status_code, 409)
        self.assertIn("Web search is turned off", refused.exception.detail)
        create.assert_not_called()


class JudgeOutputChecks(unittest.TestCase):
    """Judging asks for a compact answer, and a claim copied from its shape is never evidence."""

    def test_placeholder_or_example_claims_are_caught(self):
        from app.services.research.evidence import _PLACEHOLDER_CLAIM

        for copied in ("<the fact, in at most 25 words>", "  <claim> ",
                       "The battery lasts ten hours in the reviewer's test."):
            with self.subTest(copied=copied):
                self.assertTrue(_PLACEHOLDER_CLAIM.search(copied))
        for real in ("The GTA 6 map is roughly twice the size of GTA 5's.",
                     "GTA 6 runs at 30 fps on PS5 <and PS5 Pro>, Rockstar confirmed."):
            with self.subTest(real=real):
                self.assertFalse(_PLACEHOLDER_CLAIM.search(real))

    def test_judging_asks_for_only_what_is_used(self):
        # A 3B model answering every field and every low-relevance finding wrote 4,096 tokens
        # in 19 minutes and was cut off; asked for the compact form it wrote 492 in under three.
        from app.services.research.evidence import JUDGE_INSTRUCTIONS

        self.assertIn("only findings whose relevance is high or medium", JUDGE_INSTRUCTIONS)
        self.assertIn("Leave out any field that is empty", JUDGE_INSTRUCTIONS)
        self.assertIn("never copy it", JUDGE_INSTRUCTIONS)


class ClaimTagChecks(unittest.TestCase):
    """A supported fact is not lost because the writer tagged an extra entity on it."""

    def setUp(self):
        from app.services.research.planner import PlanDraft, validate_plan
        from app.services.research.types import ClaimFit, EvidenceItem, Relevance, Support

        query = "what changes in gta 6 compared to gta 5 on ps5"
        self.plan = validate_plan(PlanDraft.model_validate({
            "task_type": "comparison",
            "entities": [{"id": "gta5", "name": "Grand Theft Auto V", "aliases": ["GTA V"]},
                         {"id": "gta6", "name": "Grand Theft Auto VI", "aliases": ["GTA VI"]}],
            "dimensions": [{"id": "m1", "name": "Changes", "justification_refs": ["query"]}],
            "targets": [{"id": "t1", "text": "What changes in GTA VI compared to GTA V on PS5?",
                         "dimension_id": "m1", "tier": "core", "form": "single",
                         "subject_ids": ["gta5", "gta6"]}],
        }), query, DepthMode.QUICK)
        target = self.plan.top_targets()[0]
        self.evidence = {"ev1": EvidenceItem(
            id="ev1", chunk_id="s1-c1", source_id="s1", question_id=target.id,
            entity_ids=["gta6"], claim="GTA VI has two protagonists, Lucia and Jason.",
            excerpt="GTA VI has two confirmed protagonists, Lucia Caminos and Jason Duval.",
            status="accepted", relevance=Relevance.HIGH, support=Support.DIRECT,
            claim_fit=ClaimFit.SUPPORTS,
        )}
        self.question = target.id

    def claim(self, claim_type="fact"):
        from app.services.research.types import Claim

        return Claim(id="c1", text="GTA VI has two confirmed protagonists, Lucia and Jason.",
                     claim_type=claim_type, question_id=self.question,
                     entity_ids=["gta6", "gta5"], evidence_ids=["ev1"])

    def test_an_extra_tag_comes_off_a_supported_fact(self):
        from app.services.research.verify import _deterministic_checks

        claim = self.claim()
        self.assertIsNone(_deterministic_checks(self.plan, claim, self.evidence))
        self.assertEqual(claim.entity_ids, ["gta6"])

    def test_a_comparison_still_needs_evidence_for_both_sides(self):
        from app.services.research.verify import _deterministic_checks

        reason = _deterministic_checks(self.plan, self.claim("comparison"), self.evidence)
        self.assertIsNotNone(reason)


def suggested_plan(depth=DepthMode.STANDARD):
    """The plan a preview would show, made by the planner's own validation."""
    return validate_plan(PlanDraft.model_validate({
        "task_type": "historical_research",
        "entities": [{"id": "e1", "name": "House Stark", "aliases": ["Starks"]}],
        "dimensions": [
            {"id": "m1", "name": "Origins", "description": "How the family began",
             "justification_refs": ["query"]},
            {"id": "m2", "name": "Fate in the story", "description": "What happens to it",
             "justification_refs": ["query"]},
        ],
        "targets": [
            {"id": "t1", "text": "How did House Stark begin in Game of Thrones?",
             "dimension_id": "m1", "tier": "core", "subject_ids": ["e1"]},
            {"id": "t2", "text": "What happens to House Stark by the end of the story?",
             "dimension_id": "m2", "tier": "core", "subject_ids": ["e1"]},
            {"id": "t3", "text": "Which castles did House Stark hold over its history?",
             "dimension_id": "m1", "tier": "supporting", "subject_ids": ["e1"]},
        ],
    }), QUERY, depth).model_dump(mode="json")


class PlanEditChecks(unittest.TestCase):
    def test_edits_are_kept_and_checked(self):
        edited = suggested_plan()
        targets = [t for t in edited["targets"] if t["id"] != "t3"]  # removed
        targets[0]["text"] = "How was House Stark founded, according to the books and show?"
        targets[1]["tier"] = "supporting"
        targets.append({"id": "u1", "text": "Who rules the North after the Starks?",
                        "dimension_id": "m2", "tier": "core"})
        targets.append({"id": "u2", "text": "What are the Stark family words and sigil?",
                        "dimension_id": "gone", "tier": "supporting"})
        targets.append({"id": "u3", "text": "What does this query want to know?",
                        "dimension_id": "m1", "tier": "supporting"})
        edited["targets"] = targets
        plan = plan_from_edit(edited, QUERY, DepthMode.STANDARD)
        by_text = {t.text: t for t in plan.top_targets()}
        self.assertIn("How was House Stark founded, according to the books and show?", by_text)
        self.assertNotIn("Which castles did House Stark hold over its history?", by_text)
        self.assertEqual(by_text["What happens to House Stark by the end of the story?"].tier,
                         Tier.SUPPORTING)
        self.assertEqual(by_text["Who rules the North after the Starks?"].tier, Tier.CORE)
        # A question placed under an aspect that no longer exists still gets researched.
        self.assertIn("What are the Stark family words and sigil?", by_text)
        # The planner's rule against questions about the research itself still applies.
        self.assertNotIn("What does this query want to know?", by_text)
        self.assertTrue(any(issue.code == "plan_edited_by_user" for issue in plan.issues))

    def test_the_modes_caps_still_apply(self):
        edited = suggested_plan(DepthMode.QUICK)
        edited["targets"] += [
            {"id": f"u{i}", "text": f"Which Stark ruled Winterfell in era number {i}?",
             "dimension_id": "m1", "tier": "core"}
            for i in range(6)
        ]
        plan = plan_from_edit(edited, QUERY, DepthMode.QUICK)
        core = [t for t in plan.top_targets() if t.tier == Tier.CORE]
        self.assertLessEqual(len(core), 3)

    def test_a_plan_with_nothing_left_is_refused(self):
        edited = suggested_plan()
        edited["targets"] = []
        with self.assertRaises(PlanningFailed):
            plan_from_edit(edited, QUERY, DepthMode.STANDARD)

    def test_the_run_researches_the_given_plan_without_planning(self):
        from app.services.provider_runtime import pace
        from app.services.research.capability import ModelCapability
        from app.services.research.engine import EngineDependencies, _Run

        plan = plan_from_edit(suggested_plan(), QUERY, DepthMode.STANDARD)
        stages = []

        class Model:
            def chat_with_metadata(self, messages, *args, **kwargs):
                stages.append(messages[0].content.split("\n", 1)[0])
                raise RuntimeError("no model in this check")

        deps = EngineDependencies(
            llm_factory=lambda n: Model(), providers=[], fetcher=None, embed=None,
            capability=lambda: ModelCapability(model="m", max_output_tokens=2048,
                                               context_window=40960, known=True),
        )
        with patch.object(pace, "measured", return_value=None):
            run = _Run(QUERY, DepthMode.STANDARD, deps, None, lambda: False, "", "r", plan=plan)
            self.assertEqual(run.shape.planning_calls, 0)
            try:
                run.execute()
            except Exception:
                pass  # what happens after planning needs a model; only planning is checked
        self.assertIs(run.plan, plan)
        self.assertFalse([s for s in stages if s.startswith(("Stage: research_plan",
                                                              "Stage: research_task"))])


if __name__ == "__main__":
    unittest.main(verbosity=2)
