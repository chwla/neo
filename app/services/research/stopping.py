"""Stopping: whether any remaining research could materially change the answer.

The loop ends when no pursued target has a material need with a viable action, when nothing
has improved for two steps, or when a hard ceiling refuses the next step. A count of sources is
never a reason to stop, and an unused allowance is never a reason to continue.

Completion is decided afterwards, from the verified claims, with one precedence order, so the
statuses cannot overlap and every mode means the same thing by each of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.research import strategy
from app.services.research.evidence_map import EvidenceMap
from app.services.research.policy import ResearchPolicy
from app.services.research.types import (
    CompletionStatus,
    ContradictionGroup,
    ContradictionStatus,
    LeadStatus,
    OriginLead,
    Sufficiency,
    TargetStatus,
    Tier,
)

#: Steps in a row without any target improving before research is considered stalled.
GLOBAL_STALL_STEPS = 2


@dataclass
class LoopDecision:
    stop: bool
    #: "saturated", "no_progress", "ceiling" or "blocked" when stopping.
    loop_end: str = ""
    detail: str = ""


def decide(
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
    *,
    can_afford_step: bool,
    limiting_ceiling: str,
    search_unavailable: bool,
    any_evidence: bool,
) -> LoopDecision:
    if search_unavailable and not any_evidence:
        return LoopDecision(True, "blocked", "no web search provider answered")
    actionable = [
        target
        for target in emap.pursued()
        if strategy.has_material_action(target, emap, policy, leads, groups)
    ]
    if not actionable:
        return LoopDecision(True, "saturated", "no remaining research could change the answer")
    if emap.steps_without_change >= GLOBAL_STALL_STEPS:
        return LoopDecision(True, "no_progress", "the last steps improved no target")
    if not can_afford_step:
        core_left = any(target.tier == Tier.CORE for target in actionable)
        return LoopDecision(True, "ceiling" if core_left else "saturated", limiting_ceiling)
    return LoopDecision(False)


def settle_targets(
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
) -> None:
    """Name what research left each target as, once no more research will happen."""
    for target in emap.plan.targets:
        state = emap.state(target.id)
        if target.tier not in policy.pursued_tiers:
            if state.sufficiency == Sufficiency.INSUFFICIENT:
                state.status = TargetStatus.DEFERRED
            continue
        if state.status in (TargetStatus.ESTABLISHED, TargetStatus.CONTESTED):
            continue
        family = emap.family(target)
        routes = [lead for lead in leads if lead.target_id in family]
        if (
            routes
            and all(lead.status == LeadStatus.UNREADABLE for lead in routes)
            and not state.evidence_ids
        ):
            state.status = TargetStatus.UNRESOLVABLE
        elif state.sufficiency == Sufficiency.INSUFFICIENT:
            state.status = TargetStatus.EXHAUSTED


def completion_status(
    emap: EvidenceMap,
    policy: ResearchPolicy,
    groups: list[ContradictionGroup],
    *,
    loop_end: str,
    verified_targets: set[str],
    cancelled: bool = False,
    failed: bool = False,
    re_research_refused: bool = False,
) -> CompletionStatus:
    """The one completion status, decided after verification, by strict precedence."""
    if cancelled:
        return CompletionStatus.CANCELLED
    if failed:
        return CompletionStatus.FAILED
    if loop_end == "blocked":
        return CompletionStatus.BLOCKED
    if loop_end == "ceiling" or re_research_refused:
        return CompletionStatus.CEILING_REACHED
    pursued = [t for t in emap.plan.targets if t.tier in policy.pursued_tiers]
    for target in pursued:
        state = emap.state(target.id)
        # A conflict research could only represent, not explain, is still an open question.
        unexplained = any(
            g.question_id in emap.family(target) and g.status != ContradictionStatus.EXPLAINED
            for g in groups
        )
        if state.sufficiency != Sufficiency.ESTABLISHED or unexplained:
            return CompletionStatus.COMPLETED_WITH_GAPS
        if (
            target.tier == Tier.CORE
            and target.parent_id is None
            and not (emap.family(target) & verified_targets)
        ):
            return CompletionStatus.COMPLETED_WITH_GAPS
    return CompletionStatus.COMPLETED
