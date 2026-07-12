"""Evidence conversion keeps only small, attributable passages."""

from __future__ import annotations

from app.services.research_mode.confidence import source_quality
from app.services.research_mode.redaction import safe_text, safe_value


def from_web(web_result: dict) -> list[dict]:
    sources = {item.get("id"): item for item in web_result.get("sources", [])}
    output = []
    for item in web_result.get("evidence", []):
        source = sources.get(item.get("source_id"), {})
        quality, breakdown = source_quality(source)
        text = safe_text(item.get("evidence_text") or item.get("claim") or "", 900)
        if not text:
            continue
        output.append(
            {
                "source_type": "web",
                "source_id": str(item.get("source_id") or ""),
                "citation_label": item.get("citation_label"),
                "evidence_text": text,
                "extracted_claim": safe_text(item.get("claim") or text, 280),
                "confidence": min(1.0, float(item.get("confidence") or quality)),
                "quality_score": quality,
                "metadata": safe_value(
                    {
                        "url": (item.get("metadata") or {}).get("url"),
                        "title": source.get("title"),
                        "domain": source.get("domain"),
                        "freshness": source.get("fetched_at"),
                        "score_breakdown": breakdown,
                    }
                ),
            }
        )
    return output


def from_memory(result: dict) -> list[dict]:
    output = []
    for item in result.get("results", []):
        text = safe_text(item.get("snippet"), 700)
        if text:
            output.append(
                {
                    "source_type": "memory",
                    "source_id": item.get("memory_id"),
                    "citation_label": None,
                    "evidence_text": text,
                    "extracted_claim": safe_text(item.get("title"), 280),
                    "confidence": min(0.8, float(item.get("score") or 0.4)),
                    "quality_score": min(0.8, float(item.get("score") or 0.4)),
                    "metadata": safe_value(
                        {
                            "title": item.get("title"),
                            "memory_type": item.get("memory_type"),
                            "score_breakdown": item.get("score_breakdown", {}),
                        }
                    ),
                }
            )
    return output
