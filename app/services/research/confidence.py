"""Confidence from what the evidence map establishes, never from how much was collected.

There is one meaning of confidence. A target's confidence comes from its state in the evidence
map: decisive evidence or an original record is high; independent secondary sources that agree,
reports standing in for a general claim, or a pending reason to corroborate are medium; anything
not established is low. A claim is never more confident than its target or its own verified
support, and the answer is as confident as its weakest core target. URL, citation, source and
search counts are deliberately not inputs here.
"""

from __future__ import annotations

from app.services.research.evidence_map import EvidenceMap
from app.services.research.types import (
    Claim,
    ClaimType,
    Confidence,
    NeedKind,
    Sufficiency,
    SupportStatus,
    Tier,
)

_ORDER = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def _weakest(*levels: Confidence) -> Confidence:
    return min(levels, key=_ORDER.__getitem__)


def assess(
    emap: EvidenceMap, claims: tuple[Claim, ...] | list[Claim]
) -> tuple[Confidence, list[str]]:
    plan = emap.plan
    for claim in claims:
        claim.confidence, claim.confidence_reasons = claim_confidence(emap, claim)

    core = [t for t in plan.top_targets() if t.tier == Tier.CORE]
    if not core:
        return Confidence.LOW, ["No core research question could be investigated."]
    answer = Confidence.HIGH
    limiting: list[str] = []
    for target in core:
        state = emap.state(target.id)
        family = emap.family(target)
        answered = any(
            claim.question_id in family
            and claim.support in (SupportStatus.SUPPORTED, SupportStatus.PARTIALLY_SUPPORTED)
            and claim.claim_type != ClaimType.RECOMMENDATION
            for claim in claims
        )
        level = state.confidence if answered else Confidence.LOW
        if not answered:
            reasons = ["no verified finding answers it"]
        elif state.sufficiency == Sufficiency.INSUFFICIENT and not any(
            need.kind == NeedKind.CONFLICT_MATERIAL for need in state.material_needs()
        ):
            # A verified finding answers it, from less evidence than would establish it.
            reasons = ["answered only from limited evidence"]
        else:
            reasons = state.reasons
        if _ORDER[level] < _ORDER[Confidence.HIGH]:
            limiting.append(f"“{target.text}”: {'; '.join(reasons)}.")
        answer = _weakest(answer, level)
    established = sum(1 for t in core if emap.state(t.id).sufficiency.value != "insufficient")
    summary = f"{established} of {len(core)} core questions are established."
    return answer, [summary, *limiting[:4]]


def claim_confidence(emap: EvidenceMap, claim: Claim) -> tuple[Confidence, list[str]]:
    """A claim is as confident as its target's evidence, capped by its own verified support."""
    if claim.support != SupportStatus.SUPPORTED:
        return Confidence.LOW, [claim.support.value.replace("_", " ")]
    target = emap.plan.target(claim.question_id)
    if target is None:
        return Confidence.MEDIUM, ["draws on several questions"]
    state = emap.state(target.id)
    return state.confidence, list(state.reasons)
