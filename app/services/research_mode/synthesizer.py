"""Evidence-first claim synthesis; no model-generated unsupported facts."""

from __future__ import annotations

from app.services.research_mode.redaction import safe_text


def claims_from_evidence(evidence: list[dict], conflicts: list[dict]) -> list[dict]:
    conflicted_text = " ".join(" ".join(item.get("claims") or []) for item in conflicts).lower()
    claims = []
    for item in evidence:
        if item.get("source_type") != "web":
            continue
        claim = safe_text(item.get("extracted_claim") or item.get("evidence_text"), 300)
        if not claim:
            continue
        citation = item.get("citation_label")
        status = (
            "conflicted"
            if claim.lower() in conflicted_text
            else ("supported" if citation else "uncertain")
        )
        claims.append(
            {
                "claim": claim,
                "claim_type": "finding",
                "confidence": round(
                    min(
                        1.0,
                        (float(item.get("confidence") or 0) + float(item.get("quality_score") or 0))
                        / 2,
                    ),
                    3,
                ),
                "citation_ids": [citation] if citation else [],
                "evidence_ids": [item["id"]] if item.get("id") else [],
                "status": status,
                "metadata": {"source_type": item.get("source_type")},
            }
        )
    return claims[:12]
