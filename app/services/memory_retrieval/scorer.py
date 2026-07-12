from __future__ import annotations

from datetime import UTC, datetime


def _age_days(value: str | None) -> float:
    if not value:
        return 3650.0
    try:
        return max(0.0, (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds() / 86400)
    except ValueError:
        return 3650.0


def score(
    item: dict, query: str, *, scope_type: str | None, scope_id: str | None, tags: list[str]
) -> dict:
    terms = {term.lower() for term in query.split() if len(term) > 1}
    haystack = f"{item['title']} {item['content_text']} {' '.join(item.get('tags', []))}".lower()
    keyword = min(0.45, 0.08 * sum(term in haystack for term in terms))
    scope = (
        0.22
        if scope_id and item["scope_id"] == scope_id
        else (0.08 if scope_type and item["scope_type"] == scope_type else 0.0)
    )
    importance = 0.04 * int(item.get("importance", 3))
    recency = max(0.0, 0.12 * (1 - min(_age_days(item.get("updated_at")) / 365, 1)))
    access = min(0.05, 0.01 * int(item.get("access_count", 0)))
    tags_score = min(0.08, 0.04 * len(set(tags) & set(item.get("tags", []))))
    type_boost = (
        0.06
        if item.get("memory_type") in {"failure", "constraint", "safety_note"}
        and any(term in terms for term in {"fail", "failure", "constraint", "safe", "safety"})
        else 0.0
    )
    breakdown = {
        "keyword": round(keyword, 3),
        "scope": round(scope, 3),
        "importance": round(importance, 3),
        "recency": round(recency, 3),
        "access": round(access, 3),
        "tags": round(tags_score, 3),
        "type": round(type_boost, 3),
    }
    return {"score": round(min(1.0, sum(breakdown.values())), 3), "score_breakdown": breakdown}
