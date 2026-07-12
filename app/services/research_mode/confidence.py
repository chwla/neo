"""Transparent confidence calculations."""

from __future__ import annotations


def source_quality(source: dict) -> tuple[float, dict]:
    meta = source.get("metadata") or {}
    web = meta.get("score_breakdown") or {}
    official = float(web.get("official_source", 0))
    credibility = float(source.get("credibility_score") or web.get("credibility", 0.45))
    freshness = float(source.get("freshness_score") or web.get("freshness", 0.5))
    relevance = float(source.get("relevance_score") or web.get("relevance", 0.3))
    technical = float(web.get("technical_doc_boost", 0))
    clarity = 0.12 if source.get("fetched_text") or source.get("snippet") else 0.0
    score = min(
        1.0,
        round(
            0.28 * credibility
            + 0.20 * freshness
            + 0.24 * relevance
            + official
            + technical
            + clarity,
            3,
        ),
    )
    return score, {
        "official_primary": official,
        "domain_credibility": credibility,
        "freshness": freshness,
        "relevance": relevance,
        "technical_specificity": technical,
        "evidence_clarity": clarity,
        "final": score,
    }


def run_confidence(
    evidence: list[dict], claims: list[dict], conflicts: list[dict], memory_count: int
) -> dict:
    qualities = [float(item.get("quality_score") or 0) for item in evidence]
    supported = [item for item in claims if item.get("status") == "supported"]
    coverage = len(supported) / max(1, len(claims))
    quality = sum(qualities) / max(1, len(qualities))
    diversity = min(
        1.0,
        len(
            {(item.get("metadata") or {}).get("domain", item.get("source_id")) for item in evidence}
        )
        / 4,
    )
    conflict_penalty = min(
        0.45,
        sum(
            {"low": 0.05, "medium": 0.14, "high": 0.28}.get(item.get("severity"), 0.08)
            for item in conflicts
        ),
    )
    score = max(
        0.0,
        min(
            1.0,
            round(
                0.45 * quality
                + 0.25 * coverage
                + 0.15 * diversity
                + (0.05 if memory_count else 0)
                - conflict_penalty,
                3,
            ),
        ),
    )
    return {
        "overall": score,
        "source_quality": round(quality, 3),
        "citation_coverage": round(coverage, 3),
        "source_diversity": round(diversity, 3),
        "memory_corroboration": bool(memory_count),
        "conflict_penalty": round(conflict_penalty, 3),
    }
