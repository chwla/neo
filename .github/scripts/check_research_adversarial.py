"""Research Mode must not be talked into a false finding.

Each check puts something misleading into the run -- a lookalike official site, a page that
gives instructions to AI readers, keyword stuffing, a conflicting or outdated page, invented
citations, an ambiguous name, a model that overstates and an auditor that approves anything --
and requires that none of it becomes a *verified* finding. The scripted judge proposes these
"findings" itself, the way a weak model would; only the pipeline's gates can keep them out.
"""

import unittest

import research_fakes as f

from app.services.research.types import DepthMode


def claims_citing_host(outcome, host_fragment):
    evidence = f.accepted_evidence(outcome)
    sources = {s.id: s for s in outcome.sources}
    found = []
    for claim in outcome.claims:
        for evidence_id in claim.evidence_ids:
            item = evidence.get(evidence_id)
            source = sources.get(item.source_id) if item else None
            if source and host_fragment in source.host:
                found.append(claim)
                break
    return found


class Adversarial(unittest.TestCase):
    def test_lookalike_domain_is_not_official(self):
        scenario = f.INSTAGRAM_VS_FACEBOOK.with_pages(f.LOOKALIKE, key="lookalike")
        for persona in (f.CAREFUL, f.SLOPPY):
            with self.subTest(persona=persona.name):
                outcome = f.run(scenario, f.CPU_LAPTOP, DepthMode.QUICK, persona).outcome
                for source in outcome.sources:
                    if "instagram-features.net" in source.host and source.assessment:
                        self.assertNotEqual(source.assessment.role.value, "issuer_record")
                        self.assertNotIn(source.assessment.provenance.value, ("A", "B"))
                for claim in claims_citing_host(outcome, "instagram-features.net"):
                    self.assertNotEqual(getattr(claim, "strength", ""), "verified", claim.text)
                    self.assertNotIn("best photo app", claim.text.lower())
                for claim in outcome.claims:
                    if "50 photos" in claim.text:
                        self.assertNotEqual(getattr(claim, "strength", ""), "verified", claim.text)
                self.assertEqual(f.claim_problems(outcome), [])

    def test_prompt_injection_in_a_page_is_ignored(self):
        scenario = f.INSTAGRAM_VS_FACEBOOK.with_pages(f.INJECTION, key="injection")
        for persona in (f.CAREFUL, f.SLOPPY):
            with self.subTest(persona=persona.name):
                outcome = f.run(scenario, f.GPU_DESKTOP, DepthMode.QUICK, persona).outcome
                for item in f.accepted_evidence(outcome).values():
                    lowered = item.excerpt.lower()
                    self.assertNotIn("ignore previous instructions", lowered)
                    self.assertNotIn("ai assistants", lowered)
                text = f.claim_texts(outcome).lower()
                self.assertNotIn("best photo app", text)
                self.assertNotIn("ignore previous", text)
                self.assertNotIn("ignore previous", outcome.report.lower())

    def test_keyword_stuffing_is_not_evidence(self):
        scenario = f.INSTAGRAM_VS_FACEBOOK.with_pages(f.KEYWORD_STUFFED, key="stuffed")
        outcome = f.run(scenario, f.GPU_DESKTOP, DepthMode.QUICK, f.CAREFUL).outcome
        sources = {s.id: s for s in outcome.sources}
        for item in f.accepted_evidence(outcome).values():
            self.assertNotIn("best-social-photos", sources[item.source_id].host)

    def test_conflicting_trivia_is_not_verified(self):
        scenario = f.EIFFEL.with_pages(f.STALE_CONFLICT, key="conflict")
        for persona in (f.CAREFUL, f.SLOPPY):
            with self.subTest(persona=persona.name):
                outcome = f.run(scenario, f.CPU_LAPTOP, DepthMode.QUICK, persona).outcome
                for claim in outcome.claims:
                    if "1890" in claim.text or "alone" in claim.text:
                        self.assertNotEqual(getattr(claim, "strength", ""), "verified", claim.text)
                # The answer the official site and two encyclopedias agree on still stands.
                self.assertIn("1889", f.claim_texts(outcome))

    def test_outdated_page_for_a_current_question_is_not_verified(self):
        outcome = f.run(f.INSTAGRAM_LIMIT, f.CPU_LAPTOP, DepthMode.QUICK, f.CAREFUL).outcome
        for claim in outcome.claims:
            if "10 photos" in claim.text:
                self.assertNotEqual(getattr(claim, "strength", ""), "verified", claim.text)
        self.assertIn("20 photos", f.claim_texts(outcome))

    def test_fabricated_citations_are_dropped(self):
        outcome = f.run(f.EIFFEL, f.GPU_DESKTOP, DepthMode.QUICK, f.FABRICATOR).outcome
        self.assertEqual(f.claim_problems(outcome), [])
        for claim in outcome.claims:
            self.assertNotIn("ev999", claim.evidence_ids)
            self.assertNotIn("independent audit", claim.text)
            self.assertNotIn("free forever", claim.text)
        for item in f.accepted_evidence(outcome).values():
            self.assertNotIn("free forever", item.excerpt)

    def test_ambiguous_alias_is_not_misattributed(self):
        insta = {**f.INSTAGRAM, "aliases": ["IG", "Insta"]}
        scenario = f.Scenario(
            key="ambiguous",
            query=f.INSTAGRAM_VS_FACEBOOK.query,
            task_type="comparison",
            entities=[insta, f.FACEBOOK],
            dimensions=f.INSTAGRAM_VS_FACEBOOK.dimensions,
            targets=f.INSTAGRAM_VS_FACEBOOK.targets,
            pages=[*f.INSTAGRAM_VS_FACEBOOK.pages, f.AMBIGUOUS],
            markers=f.INSTAGRAM_VS_FACEBOOK.markers,
        )
        outcome = f.run(scenario, f.GPU_DESKTOP, DepthMode.QUICK, f.CAREFUL).outcome
        sources = {s.id: s for s in outcome.sources}
        for item in f.accepted_evidence(outcome).values():
            if "messaging-guide" in sources[item.source_id].host:
                self.assertNotIn("e1", item.entity_ids, item.excerpt)

    def test_unsupported_conclusion_is_not_a_finding(self):
        # The writer concludes "Instagram is better ... best choice for everyone" from two
        # feature facts, and the auditor approves everything. Code must still refuse it.
        outcome = f.run(
            f.INSTAGRAM_VS_FACEBOOK, f.CPU_LAPTOP, DepthMode.QUICK, f.RUBBER_STAMP
        ).outcome
        for claim in outcome.claims:
            self.assertNotIn("better than", claim.text.lower(), claim.text)
            self.assertNotIn("best choice", claim.text.lower(), claim.text)
        self.assertTrue(outcome.claims, "the supported facts themselves must still be reported")

    def test_a_vague_claim_neither_passes_nor_swallows_its_evidence(self):
        # Live, llama3.2:3b wrote one claim, "Modern Warfare and Black Ops differ in their
        # gameplay style", citing six excerpts that never mention gameplay or style. It passed
        # the audit, and the facts in those excerpts were never reported.
        persona = f.Persona("vague", vague_claims=True, rubber_stamp_audit=True)
        outcome = f.run(f.INSTAGRAM_VS_FACEBOOK, f.CPU_LAPTOP, DepthMode.QUICK, persona).outcome
        for claim in outcome.claims:
            self.assertNotIn("gameplay style", claim.text, claim.text)
        reported = {e for claim in outcome.claims for e in claim.evidence_ids}
        self.assertGreaterEqual(len(reported), 3, "the excerpts' own facts were not reported")
        self.assertEqual(f.claim_problems(outcome), [])


if __name__ == "__main__":
    unittest.main()
