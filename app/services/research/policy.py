"""Research policies: how much effort each mode may spend, and on what.

Quick, Standard and Deep are policies over one engine. They differ in which evidence needs
they pursue, for which targets, how many attempts each need may use, how broadly the planner
decomposes, how strictly claims are verified, and the safety ceilings of a run. They never
differ in what counts as sufficient evidence or in what a claim needs before it is reported:
those standards live in the evidence map and the verifier, not here.

Ceilings are limits a run must not exceed, never amounts it should use. A run stops as soon
as no remaining research could materially change its answer or its confidence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.services.research.types import (
    DepthMode,
    Intent,
    NeedKind,
    Tier,
)

CORE = frozenset({Tier.CORE})
CORE_SUPPORTING = frozenset({Tier.CORE, Tier.SUPPORTING})
ALL_TIERS = frozenset(Tier)
NONE: frozenset[Tier] = frozenset()


class Ceilings(BaseModel):
    """Safety limits for one run. Model calls are before the capability work scale.

    ``reads`` is where a run normally settles, not a guillotine: while a core question is still
    unanswered and the last step improved something, research may read on in small batches, up
    to ``reads_hard``. Every other limit here is absolute.
    """

    model_config = ConfigDict(frozen=True)

    seconds: int
    model_calls: int
    searches: int
    reads: int
    #: The most pages a run may read, however much is still unanswered.
    reads_hard: int
    steps: int
    targets_per_step: int
    judge_calls_per_step: int


class PlanningCaps(BaseModel):
    """Upper bounds on a plan's targets.

    The planner may, and for a simple question must, use fewer.
    """

    model_config = ConfigDict(frozen=True)

    core: int
    supporting: int
    context: int
    guidance: str


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    mode: DepthMode
    ceilings: Ceilings
    planning: PlanningCaps
    #: Model calls that may add targets once the first targets are saturated.
    expansion_calls: int
    #: Standard expands only when a target is contested or evidence reveals a prerequisite.
    expansion_requires_cause: bool
    pursued_tiers: frozenset[Tier]
    #: For each kind of need, the target tiers whose needs of that kind this mode pursues.
    #: A need is pursued only when it is also material; see the stopping rules.
    pursued_needs: dict[NeedKind, frozenset[Tier]]
    #: Tiers whose `upgrade_available` needs may be pursued by searching, not only by
    #: following a lead already found.
    upgrade_search_tiers: frozenset[Tier]
    #: Whether step 1 searches for an originating record when the planner expects one.
    step1_origin_search: bool
    #: Searches per target in step 1. With one, an expected origin is searched for in place of
    #: a direct answer, the query written to find both.
    step1_actions_per_target: int
    #: Most attempts of each intent a single target may use while a pursued need remains.
    intent_allowance: dict[Intent, int]
    entailment: Literal["core_decisive", "all", "all_strict"]
    conflict_cause_analysis: bool
    stall_window: int
    report_shape: Literal["brief", "standard", "extended"]

    @property
    def stronger_source_substitution(self) -> bool:
        return self.entailment in ("all", "all_strict")

    @property
    def union_check(self) -> bool:
        return self.entailment == "all_strict"


def _needs(
    *,
    provenance: frozenset[Tier],
    upgrade: frozenset[Tier],
    corroboration: frozenset[Tier],
    minor_conflicts: frozenset[Tier],
    pursued: frozenset[Tier],
) -> dict[NeedKind, frozenset[Tier]]:
    # Needs that decide whether an answer can be given at all are pursued for every target a
    # mode pursues; the rest are where the modes differ.
    return {
        NeedKind.INSUFFICIENT: pursued,
        NeedKind.SUBJECT_IMBALANCE: pursued,
        NeedKind.STALE: pursued,
        NeedKind.SCOPE_UNRESOLVED: pursued,
        NeedKind.CONFLICT_MATERIAL: pursued,
        NeedKind.PROVENANCE_UNCONFIRMED: provenance,
        NeedKind.UPGRADE_AVAILABLE: upgrade,
        NeedKind.CORROBORATION_HELPFUL: corroboration,
        NeedKind.CONFLICT_MINOR: minor_conflicts,
    }


POLICIES: dict[DepthMode, ResearchPolicy] = {
    DepthMode.QUICK: ResearchPolicy(
        mode=DepthMode.QUICK,
        ceilings=Ceilings(
            seconds=600,
            model_calls=16,
            searches=12,
            reads=12,
            reads_hard=20,
            steps=4,
            targets_per_step=3,
            judge_calls_per_step=1,
        ),
        planning=PlanningCaps(
            core=3,
            supporting=1,
            context=0,
            guidance="Include only what the core answer depends on.",
        ),
        expansion_calls=0,
        expansion_requires_cause=True,
        pursued_tiers=CORE,
        pursued_needs=_needs(
            provenance=CORE,
            upgrade=CORE,
            corroboration=NONE,
            minor_conflicts=NONE,
            pursued=CORE,
        ),
        upgrade_search_tiers=NONE,
        # Quick's one first search per target asks for the answer itself. Hunting the record
        # first spends the whole step on pages that are often paywalled, and leaves a quick
        # run nothing to answer from; records found on the pages it reads are still followed.
        step1_origin_search=False,
        step1_actions_per_target=1,
        # Quick follows records already cited on pages it reads, but never hunts for one: its
        # searches go to the answer, asked a second way if the first found too little.
        intent_allowance={
            Intent.DIRECT_ANSWER: 2,
            Intent.LOCATE_ORIGIN: 0,
            Intent.FOLLOW_LEAD: 2,
            Intent.ACCESSIBLE_COPY: 0,
            Intent.CORROBORATE: 1,
            Intent.RESOLVE_CONFLICT: 1,
            Intent.BROADEN: 0,
        },
        entailment="core_decisive",
        conflict_cause_analysis=False,
        stall_window=1,
        report_shape="brief",
    ),
    DepthMode.STANDARD: ResearchPolicy(
        mode=DepthMode.STANDARD,
        ceilings=Ceilings(
            seconds=1800,
            model_calls=36,
            searches=30,
            reads=30,
            reads_hard=50,
            steps=8,
            targets_per_step=4,
            judge_calls_per_step=2,
        ),
        planning=PlanningCaps(
            core=6,
            supporting=4,
            context=0,
            guidance="Cover every major aspect of the question.",
        ),
        expansion_calls=1,
        expansion_requires_cause=True,
        pursued_tiers=CORE_SUPPORTING,
        pursued_needs=_needs(
            provenance=CORE_SUPPORTING,
            upgrade=CORE_SUPPORTING,
            corroboration=CORE,
            minor_conflicts=frozenset({Tier.SUPPORTING}),
            pursued=CORE_SUPPORTING,
        ),
        upgrade_search_tiers=CORE,
        step1_origin_search=True,
        step1_actions_per_target=2,
        intent_allowance={
            Intent.DIRECT_ANSWER: 2,
            Intent.LOCATE_ORIGIN: 2,
            Intent.FOLLOW_LEAD: 3,
            Intent.ACCESSIBLE_COPY: 1,
            Intent.CORROBORATE: 2,
            Intent.RESOLVE_CONFLICT: 2,
            Intent.BROADEN: 1,
        },
        entailment="all",
        conflict_cause_analysis=False,
        stall_window=1,
        report_shape="standard",
    ),
    DepthMode.DEEP: ResearchPolicy(
        mode=DepthMode.DEEP,
        ceilings=Ceilings(
            seconds=4500,
            model_calls=80,
            searches=72,
            reads=70,
            reads_hard=110,
            steps=16,
            targets_per_step=6,
            judge_calls_per_step=3,
        ),
        planning=PlanningCaps(
            core=8,
            supporting=6,
            context=4,
            guidance=(
                "Cover every major aspect. Where the question has them, also include competing "
                "explanations, methods, history and current context; never add such targets "
                "when the question has none."
            ),
        ),
        expansion_calls=2,
        expansion_requires_cause=False,
        pursued_tiers=ALL_TIERS,
        pursued_needs=_needs(
            provenance=ALL_TIERS,
            upgrade=ALL_TIERS,
            corroboration=CORE_SUPPORTING,
            minor_conflicts=ALL_TIERS,
            pursued=ALL_TIERS,
        ),
        upgrade_search_tiers=CORE_SUPPORTING,
        step1_origin_search=True,
        step1_actions_per_target=2,
        intent_allowance={
            Intent.DIRECT_ANSWER: 2,
            Intent.LOCATE_ORIGIN: 3,
            Intent.FOLLOW_LEAD: 5,
            Intent.ACCESSIBLE_COPY: 2,
            Intent.CORROBORATE: 3,
            Intent.RESOLVE_CONFLICT: 3,
            Intent.BROADEN: 1,
        },
        entailment="all_strict",
        conflict_cause_analysis=True,
        stall_window=2,
        report_shape="extended",
    ),
}
