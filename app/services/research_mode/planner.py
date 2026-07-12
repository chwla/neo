"""Deterministic, bounded planning for research investigations."""

from __future__ import annotations

import re

from app.services.research_mode.types import ResearchPlan


def make_plan(
    question: str, mode: str = "general", freshness_required: bool = True, depth: str = "standard"
) -> ResearchPlan:
    normalized = re.sub(r"\s+", " ", question).strip()
    terms = [
        item for item in re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]+", normalized) if len(item) > 2
    ][:5]
    subject = " ".join(terms) or "the research question"
    count = {"quick": 2, "standard": 3, "deep": 4}.get(depth, 3)
    subquestions = [
        f"What primary or official evidence answers: {normalized}?",
        f"What constraints, trade-offs, or implementation details materially affect {subject}?",
        "Which recent sources disagree, and what explains the difference?",
        "What remains unknown or requires a decision by the requester?",
    ][:count]
    queries = [
        normalized,
        f"{subject} official documentation",
        f"{subject} limitations tradeoffs",
        f"{subject} independent analysis disagreement",
    ][:count]
    if freshness_required:
        queries[-1] = f"{subject} current release changes"
    return ResearchPlan(
        question=normalized,
        intent=mode,  # type: ignore[arg-type]
        freshness_required=freshness_required,
        objective=f"Produce an evidence-grounded answer to: {normalized}",
        assumptions=[
            "Only bounded, publicly accessible sources are used.",
            "Claims without evidence are marked uncertain rather than presented as facts.",
        ],
        subquestions=subquestions,
        required_sources=[
            "official",
            "primary",
            "recent",
            "technical_docs" if mode in {"technical", "coding"} else "independent",
        ],
        search_queries=queries,
        memory_queries=[normalized, f"prior decisions {subject}"],
        evidence_requirements=[
            "A short attributable passage for every key finding.",
            "At least one resolvable citation label per supported claim.",
        ],
        risk_notes=[
            "Search results may be unavailable or stale.",
            "Recommendations can be context-dependent and are labeled accordingly.",
        ],
        expected_conflicts=[
            "version/date mismatch",
            "official-vs-third-party mismatch",
            "recommendation disagreement",
        ],
        completion_criteria=[
            "Key factual claims have evidence and citations.",
            "Conflicts and uncertainties are disclosed.",
            "Citation validation passes before final status is completed.",
        ],
    )
