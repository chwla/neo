import unittest

from app.services.research.evidence import (
    extract_entity_terms,
    extract_evidence,
    filter_irrelevant_sources,
)
from app.services.research.planner import _extract_generic_comparison, generate_plan
from app.services.research.synthesizer import (
    _compute_confidence,
    _ensure_required_report_sections,
    _insufficient_evidence_report,
    _normalize_report_format,
    _strict_citation_cleanup,
)
from app.services.research.types import DepthMode, ResearchEvidenceChunk, ResearchSource


class GenericComparisonRegressionTests(unittest.TestCase):
    def test_preserves_multi_word_entity_boundaries(self):
        cases = [
            ("ubuntu vs linux mint", ("Ubuntu", "Linux Mint")),
            ("java vs javascript", ("Java", "JavaScript")),
            ("c vs c++", ("C", "C++")),
            ("macbook air vs macbook pro", ("MacBook Air", "MacBook Pro")),
        ]

        for query, expected in cases:
            with self.subTest(query=query):
                plan = generate_plan(query, DepthMode.QUICK)
                entities = tuple(plan.normalized_entities.values())
                self.assertEqual(expected, entities)
                self.assertNotIn("Ubuntu Mint", plan.normalized_query or "")

        generic = _extract_generic_comparison("cursor vs codex")
        self.assertIsNotNone(generic)
        self.assertEqual(("Cursor", "Codex"), (generic["left"], generic["right"]))

    def test_os_comparison_keeps_qualifier_out_of_entities(self):
        plan = generate_plan(
            "ubuntu vs linux mint for personal use operating system", DepthMode.STANDARD
        )

        self.assertEqual("operating_system", plan.domain_hint)
        self.assertEqual(("Ubuntu", "Linux Mint"), tuple(plan.normalized_entities.values()))
        self.assertIn("personal use", plan.qualifiers)
        self.assertIn("Ubuntu vs Linux Mint", plan.normalized_query or "")
        self.assertNotIn("Ubuntu Mint", plan.normalized_query or "")
        self.assertTrue(any("site:ubuntu.com Ubuntu" in q for q in plan.queries))
        self.assertTrue(any("site:linuxmint.com Linux Mint" in q for q in plan.queries))

    def test_os_filter_rejects_generic_linux_pollution(self):
        plan = generate_plan(
            "ubuntu vs linux mint for personal use operating system", DepthMode.STANDARD
        )
        sources = [
            ResearchSource(
                id=1,
                url="https://www.geeksforgeeks.org/linux-unix/linux-tutorial/",
                title="Linux/Unix Tutorial",
                domain="geeksforgeeks.org",
                fetched=True,
                fetch_status="success",
                text="Introduction to Linux operating system basics and shell commands.",
                quality_score=6.0,
            ),
            ResearchSource(
                id=2,
                url="https://linuxmint.com/documentation.php",
                title="Linux Mint Documentation",
                domain="linuxmint.com",
                fetched=True,
                fetch_status="success",
                text="Linux Mint documentation for installing and using Linux Mint on a desktop computer.",
                quality_score=6.0,
            ),
        ]

        filtered = filter_irrelevant_sources(
            sources,
            extract_entity_terms(plan.original_query or "", plan),
            plan,
            plan.original_query or "",
        )

        self.assertEqual("rejected", filtered[0].fetch_status)
        self.assertTrue(filtered[1].fetched)
        self.assertGreater(filtered[1].quality_score, 6.0)

    def test_generic_confidence_caps_unbalanced_comparison(self):
        plan = generate_plan(
            "ubuntu vs linux mint for personal use operating system", DepthMode.STANDARD
        )
        source = ResearchSource(
            id=1,
            url="https://ubuntu.com/desktop",
            title="Ubuntu Desktop",
            domain="ubuntu.com",
            fetched=True,
            fetch_status="success",
            text="Ubuntu Desktop is an operating system for desktop and laptop computers. Ubuntu Desktop has documentation and software support.",
            quality_score=9.0,
            evidence_count=1,
        )
        evidence = extract_evidence(
            [source],
            plan,
            extract_entity_terms(plan.original_query or "", plan),
            plan.original_query or "",
        )

        self.assertEqual(
            "Low",
            _compute_confidence(
                {"relevant": 5, "evidence": 12, "unique_domains": 3},
                "full",
                plan,
                evidence,
                [source],
            ),
        )

    def test_format_repair_preserves_evidence_quality_and_removes_invalid_placeholder(self):
        report = (
            "## 1. Executive Summary\n\nPoint [N]\n\n"
            "## 2. Research Scope\n\nScope\n\n"
            "## 3. Evidence Quality\n\nQuality\n\n"
            "## 4. Key Findings\n\nFinding (Source [N])\n"
        )
        cleaned = _strict_citation_cleanup(report, [], [])
        normalized = _normalize_report_format(cleaned)

        self.assertIn("## 3. Evidence Quality", normalized)
        self.assertNotIn("[N]", normalized)
        self.assertNotIn("(Source [N])", normalized)

    def test_insufficient_report_uses_required_section_format(self):
        plan = generate_plan(
            "ubuntu vs linux mint for personal use operating system", DepthMode.STANDARD
        )
        report = _insufficient_evidence_report(
            "ubuntu vs linux mint for personal use operating system",
            DepthMode.STANDARD,
            "2026-06-23 00:00 UTC",
            [],
            [],
            reason="No evidence chunks extracted",
            plan=plan,
        )

        for heading in [
            "## 1. Executive Summary",
            "## 2. Research Scope",
            "## 3. Evidence Quality",
            "## 4. Key Findings",
            "## 5. Detailed Analysis",
            "## 6. Comparison / Tradeoffs",
            "## 7. Recommendation",
            "## 8. Risks, Unknowns, and Gaps",
            "## 9. Suggested Follow-Up Research",
            "## 10. Sources",
        ]:
            self.assertIn(heading, report)
        self.assertIn("| Dimension | Ubuntu | Linux Mint | Evidence |", report)
        self.assertNotIn("Ubuntu Mint", report)

    def test_repair_replaces_malformed_os_table_and_short_recommendation(self):
        plan = generate_plan(
            "ubuntu vs linux mint for personal use operating system", DepthMode.STANDARD
        )
        evidence = [
            ResearchEvidenceChunk(
                source_id=1,
                source_url="https://ubuntu.com/desktop",
                source_title="Ubuntu Desktop",
                text="Ubuntu Desktop is available from Ubuntu official pages.",
                evidence_category="left_evidence",
            ),
            ResearchEvidenceChunk(
                source_id=2,
                source_url="https://linuxmint.com/",
                source_title="Home - Linux Mint",
                text="Linux Mint provides a desktop operating system experience.",
                evidence_category="right_evidence",
            ),
            ResearchEvidenceChunk(
                source_id=3,
                source_url="https://example.com/ubuntu-vs-linux-mint",
                source_title="Ubuntu vs Linux Mint",
                text="Ubuntu and Linux Mint are compared for desktop users.",
                evidence_category="comparison_evidence",
            ),
        ]
        report = (
            "# Ubuntu vs Linux Mint\n\n"
            "## 1. Executive Summary\n\nSummary\n\n"
            "## 2. Research Scope\n\nScope\n\n"
            "## 3. Evidence Quality\n\nQuality\n\n"
            "## 4. Key Findings\n\nFindings\n\n"
            "## 5. Detailed Analysis\n\nAnalysis\n\n"
            "## 6. Comparison / Tradeoffs\n\n"
            "| Dimension | Ubuntu | Linux Mint |\n"
            "| --- | --- | --- |\n"
            "| Ease of use | Value | Value |\n\n"
            "## 7. Recommendation\n\n**Recommendation:** Based\n\n"
            "## 8. Risks, Unknowns, and Gaps\n\nRisks\n\n"
            "## 9. Suggested Follow-Up Research\n\n1. Follow up\n"
        )

        repaired = _ensure_required_report_sections(report, plan, evidence, [])

        self.assertIn("| Dimension | Ubuntu | Linux Mint | Evidence |", repaired)
        self.assertNotIn("**Recommendation:** Based\n", repaired)
        self.assertIn("**Recommendation:**", repaired)


if __name__ == "__main__":
    unittest.main()
