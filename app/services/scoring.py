import re

HIGH_IMPORTANCE_TERMS = {
    "goal",
    "career",
    "project",
    "important",
    "priority",
    "build",
    "job",
    "internship",
}


def score_importance(text: str, explicit_priority: int | None = None) -> int:
    """Score memory importance on the 1-10 scale from durable-usefulness signals."""

    if explicit_priority is not None:
        return max(1, min(10, explicit_priority))

    normalized = text.lower()
    score = 5
    if any(term in normalized for term in HIGH_IMPORTANCE_TERMS):
        score += 3
    if re.search(r"\b(always|never|must|highest|critical)\b", normalized):
        score += 2
    if len(text) < 25:
        score -= 1
    return max(1, min(10, score))
