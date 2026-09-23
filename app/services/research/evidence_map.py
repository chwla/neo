"""The evidence map: the one account of what the evidence establishes for every target.

For each target it decides sufficiency, which is the same standard in every mode, and lists
what the evidence still lacks. Every need carries whether resolving it could change the answer
or its confidence; stopping and strategy act only on those. Confidence, the report's gaps and
completion all read this state, so no two parts of Research Mode can hold different views.

Strength never comes from how many items were collected: it comes from decisive evidence, from
independent sources converging, and from provenance. A growing pile of restatements does not
change a target's fingerprint, so it cannot look like progress.
"""

from __future__ import annotations

from collections import defaultdict

from app.services.research.authority import ISSUER_ROLES
from app.services.research.policy import ResearchPolicy
from app.services.research.textutil import compact, parse_date, scope_conflict
from app.services.research.types import (
    AuthorityKind,
    Basis,
    ClaimFit,
    ClaimScope,
    Confidence,
    ContradictionCause,
    ContradictionGroup,
    ContradictionStatus,
    ErrorProne,
    EvidenceItem,
    EvidenceTarget,
    FreshnessFit,
    Intent,
    LeadStatus,
    NeedKind,
    OriginLead,
    OriginStatus,
    Polarity,
    Provenance,
    RecencyKind,
    ResearchPlan,
    ResearchSource,
    Sufficiency,
    Support,
    TargetForm,
    TargetNeed,
    TargetState,
    TargetStatus,
    Tier,
)

#: Independent sources of provenance B or better that together establish a record claim.
CONVERGENT_B_SOURCES = 2
#: Independent sources of provenance C or better that together establish a record claim.
CONVERGENT_C_SOURCES = 3
#: Independent general-scope evaluations or datasets that together establish a general claim.
CONVERGENT_EVALUATIONS = 2
#: Independent individual reports behind a general claim stated as reports.
HEDGED_REPORTS = 3
#: Independent peer-reviewed studies that together establish a consensus claim.
CONVERGENT_STUDIES = 3

_SUFFICIENCY_RANK = {Sufficiency.INSUFFICIENT: 0, Sufficiency.HEDGED: 1, Sufficiency.ESTABLISHED: 2}
_BASIS_RANK = {Basis.NONE: 0, Basis.REPORTS: 1, Basis.CONVERGENT: 2, Basis.DECISIVE: 3}
_PROVENANCE_RANK = {None: 0, Provenance.D: 1, Provenance.C: 2, Provenance.B: 3, Provenance.A: 4}
_ORIGIN_RANK = {
    OriginStatus.NOT_SOUGHT: 0,
    OriginStatus.SOUGHT_NOT_FOUND: 0,
    OriginStatus.LOCATED_UNREADABLE: 1,
    OriginStatus.FOUND: 2,
}
_WEAK_NEGATIVES = frozenset({"copy", "promotional_structure", "poor_extraction", "issuer_conflict"})


class EvidenceMap:
    def __init__(self, plan: ResearchPlan, policy: ResearchPolicy) -> None:
        self.plan = plan
        self.policy = policy
        self.states: dict[str, TargetState] = {
            target.id: TargetState(target_id=target.id, tier=target.tier) for target in plan.targets
        }
        #: Consecutive updates in which no target's fingerprint improved.
        self.steps_without_change = 0
        self.updates = 0

    # --- reading ---------------------------------------------------------------------------

    def state(self, target_id: str) -> TargetState:
        return self.states[target_id]

    def pursued(self) -> list[EvidenceTarget]:
        """Targets this mode researches; others are answered only from evidence in hand."""
        return [t for t in self.plan.targets if t.tier in self.policy.pursued_tiers]

    def family(self, target: EvidenceTarget) -> set[str]:
        """The target, its parent and its children: evidence and leads that bear on it."""
        ids = {target.id}
        if target.parent_id:
            ids.add(target.parent_id)
        ids |= {child.id for child in self.plan.children(target.id)}
        return ids

    def summary(self) -> list[dict]:
        return [
            {
                "target_id": state.target_id,
                "text": self.plan.target(state.target_id).text,
                "sufficiency": state.sufficiency.value,
                "basis": state.basis.value,
                "confidence": state.confidence.value,
                "needs": [need.kind.value for need in state.material_needs()],
            }
            for state in self.states.values()
            if self.plan.target(state.target_id) is not None
        ]

    # --- updating --------------------------------------------------------------------------

    def update(
        self,
        evidence: list[EvidenceItem],
        sources: list[ResearchSource],
        groups: list[ContradictionGroup],
        leads: list[OriginLead],
    ) -> bool:
        """Recompute every target's state. Returns whether any target's standing improved."""
        accepted = [e for e in evidence if e.status == "accepted" and e.duplicate_of is None]
        by_source = {source.id: source for source in sources}
        mark_supersession(self.plan, accepted)
        classify_conflicts(groups, accepted, self.policy.intent_allowance[Intent.RESOLVE_CONFLICT])
        improved = False
        # Children and single targets first: a parent's standing is built from its children's.
        ordered = sorted(self.plan.targets, key=lambda t: bool(self.plan.children(t.id)))
        for target in ordered:
            state = self.states.setdefault(
                target.id, TargetState(target_id=target.id, tier=target.tier)
            )
            previous = list(state.fingerprint)
            self._assess(target, state, accepted, by_source, groups, leads)
            if _better(state.fingerprint, previous):
                state.unchanged_steps = 0
                improved = True
            else:
                state.unchanged_steps += 1
        self.updates += 1
        self.steps_without_change = 0 if improved else self.steps_without_change + 1
        return improved

    def add_targets(self, targets: list[EvidenceTarget]) -> None:
        """Take on targets the plan gained after research started."""
        for target in targets:
            self.states.setdefault(target.id, TargetState(target_id=target.id, tier=target.tier))
        self.steps_without_change = 0

    def record_attempt(self, target_id: str, intent: Intent) -> None:
        state = self.states[target_id]
        state.attempts[intent.value] = state.attempts.get(intent.value, 0) + 1

    def record_gain(self, target_id: str, intent: Intent, improved: bool) -> None:
        """After a step, remember whether an intent helped this target, for stall detection."""
        state = self.states[target_id]
        if improved:
            state.no_gain.pop(intent.value, None)
        else:
            state.no_gain[intent.value] = state.no_gain.get(intent.value, 0) + 1

    def _assess(
        self,
        target: EvidenceTarget,
        state: TargetState,
        accepted: list[EvidenceItem],
        by_source: dict[str, ResearchSource],
        groups: list[ContradictionGroup],
        leads: list[OriginLead],
    ) -> None:
        items = self._items_for(target, accepted)
        usable = [
            item
            for item in items
            if item.claim_fit in (ClaimFit.ESTABLISHES, ClaimFit.SUPPORTS)
            and not item.superseded_by
        ]
        family = self.family(target)
        open_groups = [
            g for g in groups if g.question_id in family and g.status == ContradictionStatus.OPEN
        ]
        in_open_conflict = {e for g in open_groups for e in g.evidence_ids}

        sufficiency, basis, decisive = _sufficiency(target, usable, by_source, in_open_conflict)
        children = self.plan.children(target.id)
        child_states = [self.states[c.id] for c in children if c.id in self.states]
        if children:
            sufficiency, basis = _with_children(target, sufficiency, basis, child_states)

        state.evidence_ids = [item.id for item in usable]
        state.decisive_evidence_ids = [item.id for item in decisive]
        provenances = [_provenance(item, by_source) for item in usable]
        state.best_provenance = max(provenances, key=_PROVENANCE_RANK.get, default=None)
        state.has_provenance_a = Provenance.A in provenances or any(
            s.has_provenance_a for s in child_states
        )
        b_groups = _independent(usable, by_source, (Provenance.A, Provenance.B))
        state.independent_groups = len(
            _independent(usable, by_source, (Provenance.A, Provenance.B, Provenance.C))
        )
        state.origin_status = _origin_status(state, usable, by_source, leads, family)
        state.sufficiency, state.basis = sufficiency, basis

        needs: list[TargetNeed] = []
        open_leads = [
            lead for lead in leads if lead.target_id in family and lead.status == LeadStatus.OPEN
        ]
        if sufficiency == Sufficiency.INSUFFICIENT:
            needs.append(
                TargetNeed(
                    kind=NeedKind.INSUFFICIENT,
                    material=True,
                    reason="the evidence does not yet establish an answer",
                    lead_ids=[lead.id for lead in open_leads],
                )
            )
        if children and 0 < sum(
            s.sufficiency != Sufficiency.INSUFFICIENT for s in child_states
        ) < len(child_states):
            needs.append(
                TargetNeed(
                    kind=NeedKind.SUBJECT_IMBALANCE,
                    material=True,
                    reason="some subjects are established and others are not",
                )
            )
        profile = target.profile
        if (
            sufficiency == Sufficiency.INSUFFICIENT
            and profile.recency.kind == RecencyKind.CURRENT
            and any(item.freshness_fit == FreshnessFit.STALE for item in items)
        ):
            needs.append(
                TargetNeed(
                    kind=NeedKind.STALE, material=True, reason="only outdated evidence found"
                )
            )
        if (
            sufficiency == Sufficiency.INSUFFICIENT
            and target.scope_note
            and any(scope_conflict(item.scope, target.scope_note) for item in items)
        ):
            needs.append(
                TargetNeed(
                    kind=NeedKind.SCOPE_UNRESOLVED,
                    material=True,
                    reason=f"evidence found is for a different scope than {target.scope_note}",
                )
            )
        usable_ids = {item.id for item in usable}
        for group in open_groups:
            material = bool(set(group.evidence_ids) & usable_ids) and target.tier != Tier.CONTEXT
            needs.append(
                TargetNeed(
                    kind=NeedKind.CONFLICT_MATERIAL if material else NeedKind.CONFLICT_MINOR,
                    material=True,
                    reason=f"sources disagree: {group.detail}"[:200],
                )
            )
        if sufficiency == Sufficiency.INSUFFICIENT and any(
            item.claim_fit == ClaimFit.SUPPORTS
            and item.support == Support.DIRECT
            and (source := by_source.get(item.source_id)) is not None
            and source.assessment is not None
            and source.assessment.role in ISSUER_ROLES
            and source.assessment.provenance == Provenance.B
            for item in usable
        ):
            needs.append(
                TargetNeed(
                    kind=NeedKind.PROVENANCE_UNCONFIRMED,
                    material=True,
                    reason="the best evidence is a publisher's own page nothing else vouches for",
                )
            )
        if (
            sufficiency != Sufficiency.INSUFFICIENT
            and basis != Basis.DECISIVE
            and not state.has_provenance_a
        ):
            # An answer that rests on secondary pages is an open question of provenance
            # whatever it is about, not only when it is about a document.
            if open_leads or state.origin_status != OriginStatus.FOUND:
                needs.append(
                    TargetNeed(
                        kind=NeedKind.UPGRADE_AVAILABLE,
                        material=True,
                        reason=(
                            "a cited original record has not been read yet"
                            if open_leads
                            else "the answer rests on secondary sources, not the original record"
                        ),
                        lead_ids=[lead.id for lead in open_leads],
                    )
                )
        corroboration = _corroboration_reasons(target, state, basis, sufficiency, b_groups)
        if corroboration:
            needs.append(
                TargetNeed(
                    kind=NeedKind.CORROBORATION_HELPFUL,
                    material=True,
                    reason="; ".join(corroboration),
                )
            )
        state.needs = needs

        if any(n.kind == NeedKind.CONFLICT_MATERIAL for n in needs):
            state.status = TargetStatus.CONTESTED
        elif sufficiency != Sufficiency.INSUFFICIENT:
            state.status = TargetStatus.ESTABLISHED
        elif state.status not in (TargetStatus.EXHAUSTED, TargetStatus.UNRESOLVABLE):
            state.status = TargetStatus.OPEN
        state.confidence, state.reasons = _confidence(target, state)
        state.fingerprint = [
            _SUFFICIENCY_RANK[sufficiency],
            _BASIS_RANK[basis],
            _PROVENANCE_RANK[state.best_provenance],
            min(state.independent_groups, 3),
            _ORIGIN_RANK[state.origin_status],
            -len(open_groups),
            len(open_leads),
        ]

    def _items_for(
        self, target: EvidenceTarget, accepted: list[EvidenceItem]
    ) -> list[EvidenceItem]:
        own = [item for item in accepted if item.question_id == target.id]
        if target.parent_id and target.subject_ids:
            subject = target.subject_ids[0]
            own += [
                item
                for item in accepted
                if item.question_id == target.parent_id and item.entity_ids == [subject]
            ]
        return own


# --- sufficiency -----------------------------------------------------------------------------


def _provenance(item: EvidenceItem, by_source: dict[str, ResearchSource]) -> Provenance | None:
    source = by_source.get(item.source_id)
    return source.assessment.provenance if source is not None and source.assessment else None


def _independent(
    items: list[EvidenceItem],
    by_source: dict[str, ResearchSource],
    grades: tuple[Provenance, ...],
    predicate=lambda item, source: True,
) -> set[str]:
    """How many separate lines of evidence agree, not how many pages repeat one.

    Pages that cite the same record are quoting one source between them, however many of them
    there are and however different their publishers: they count once, because what they agree
    on is what that record said.
    """
    keys: set[str] = set()
    for item in items:
        source = by_source.get(item.source_id)
        if source is None or source.assessment is None:
            continue
        if source.assessment.provenance not in grades:
            continue
        if set(source.assessment.negative) & _WEAK_NEGATIVES:
            continue
        if predicate(item, source):
            upstream = min(item.lead_ids) if item.lead_ids else ""
            keys.add(f"cites:{upstream}" if upstream else source.independence_key)
    return keys


def _sufficiency(
    target: EvidenceTarget,
    usable: list[EvidenceItem],
    by_source: dict[str, ResearchSource],
    in_open_conflict: set[str],
) -> tuple[Sufficiency, Basis, list[EvidenceItem]]:
    """The one sufficiency standard, identical in every mode."""
    agreeing = [item for item in usable if item.id not in in_open_conflict]
    decisive = [
        item
        for item in agreeing
        if item.claim_fit == ClaimFit.ESTABLISHES and item.support == Support.DIRECT
    ]
    if decisive:
        return Sufficiency.ESTABLISHED, Basis.DECISIVE, decisive
    profile = target.profile
    if profile.claim_scope in (ClaimScope.RECORD, ClaimScope.INSTANCE):
        strong = _independent(agreeing, by_source, (Provenance.A, Provenance.B))
        fair = _independent(agreeing, by_source, (Provenance.A, Provenance.B, Provenance.C))
        if len(strong) >= CONVERGENT_B_SOURCES or len(fair) >= CONVERGENT_C_SOURCES:
            return Sufficiency.ESTABLISHED, Basis.CONVERGENT, []
        return Sufficiency.INSUFFICIENT, Basis.NONE, []
    if profile.authority_kind == AuthorityKind.CONSENSUS:
        studies = _independent(
            agreeing,
            by_source,
            (Provenance.A, Provenance.B),
            lambda item, source: source.assessment.review_status == "peer_reviewed",
        )
        if len(studies) >= CONVERGENT_STUDIES and not in_open_conflict:
            return Sufficiency.ESTABLISHED, Basis.CONVERGENT, []
        return Sufficiency.INSUFFICIENT, Basis.NONE, []
    evaluations = _independent(
        agreeing,
        by_source,
        (Provenance.A, Provenance.B, Provenance.C),
        lambda item, source: (
            item.evidence_scope == ClaimScope.GENERAL
            and item.method_stated
            and not item.interested_party
        ),
    )
    if len(evaluations) >= CONVERGENT_EVALUATIONS:
        return Sufficiency.ESTABLISHED, Basis.CONVERGENT, []
    reports = _independent(
        agreeing,
        by_source,
        (Provenance.A, Provenance.B, Provenance.C),
        lambda item, source: (
            (item.single_report or item.evidence_scope == ClaimScope.INSTANCE)
            and item.polarity == Polarity.AFFIRMS
        ),
    )
    contrary = any(
        (item.single_report or item.evidence_scope == ClaimScope.INSTANCE)
        and item.polarity == Polarity.NEGATES
        for item in agreeing
    )
    if len(reports) >= HEDGED_REPORTS and not contrary:
        return Sufficiency.HEDGED, Basis.REPORTS, []
    return Sufficiency.INSUFFICIENT, Basis.NONE, []


def _with_children(
    target: EvidenceTarget,
    own: Sufficiency,
    own_basis: Basis,
    children: list[TargetState],
) -> tuple[Sufficiency, Basis]:
    """A per-subject target stands on its subjects; a joint one also on direct comparisons."""
    if not children:
        return own, own_basis
    settled = [c for c in children if c.sufficiency != Sufficiency.INSUFFICIENT]
    if len(settled) == len(children):
        weakest_basis = min((c.basis for c in children), key=_BASIS_RANK.get)
        combined = (
            Sufficiency.HEDGED
            if any(c.sufficiency == Sufficiency.HEDGED for c in children)
            else Sufficiency.ESTABLISHED
        )
        if target.form == TargetForm.JOINT and own != Sufficiency.INSUFFICIENT:
            better = (
                own_basis if _BASIS_RANK[own_basis] > _BASIS_RANK[weakest_basis] else weakest_basis
            )
            return Sufficiency.ESTABLISHED, better
        return combined, weakest_basis
    if target.form == TargetForm.JOINT:
        return own, own_basis
    return Sufficiency.INSUFFICIENT, Basis.NONE


def _origin_status(
    state: TargetState,
    usable: list[EvidenceItem],
    by_source: dict[str, ResearchSource],
    leads: list[OriginLead],
    family: set[str],
) -> OriginStatus:
    for item in usable:
        source = by_source.get(item.source_id)
        if (
            source is not None
            and source.assessment is not None
            and source.assessment.role in ISSUER_ROLES
            and source.assessment.provenance == Provenance.A
        ):
            return OriginStatus.FOUND
    if any(lead.target_id in family and lead.status == LeadStatus.UNREADABLE for lead in leads):
        return OriginStatus.LOCATED_UNREADABLE
    sought = state.attempts.get(Intent.LOCATE_ORIGIN.value, 0) + state.attempts.get(
        Intent.FOLLOW_LEAD.value, 0
    )
    return OriginStatus.SOUGHT_NOT_FOUND if sought else OriginStatus.NOT_SOUGHT


def _corroboration_reasons(
    target: EvidenceTarget,
    state: TargetState,
    basis: Basis,
    sufficiency: Sufficiency,
    strong_groups: set[str],
) -> list[str]:
    """Concrete reasons a settled answer could still be wrong or incomplete. None for a decisive
    record: the strongest source that can exist has already spoken."""
    if sufficiency == Sufficiency.INSUFFICIENT:
        return []
    profile = target.profile
    corroborated = len(strong_groups) >= 2
    reasons: list[str] = []
    if (
        profile.authority_kind in (AuthorityKind.EVALUATIVE, AuthorityKind.CONSENSUS)
        and profile.claim_scope == ClaimScope.GENERAL
        and not corroborated
    ):
        reasons.append("a general judgement rests on one independent source")
    if profile.error_prone != ErrorProne.NONE and not corroborated:
        reasons.append(f"the figure is an {profile.error_prone.value.replace('_', ' ')}")
    if basis == Basis.CONVERGENT and not state.has_provenance_a and not corroborated:
        reasons.append("only lower-provenance sources agree")
    if sufficiency == Sufficiency.HEDGED:
        reasons.append("only individual reports support a general claim")
    return reasons


def _confidence(target: EvidenceTarget, state: TargetState) -> tuple[Confidence, list[str]]:
    material = {need.kind for need in state.material_needs()}
    if state.sufficiency == Sufficiency.INSUFFICIENT or NeedKind.CONFLICT_MATERIAL in material:
        reason = (
            "sources conflict on it"
            if NeedKind.CONFLICT_MATERIAL in material
            else "the evidence does not establish it"
        )
        return Confidence.LOW, [reason]
    reasons: list[str] = []
    if state.sufficiency == Sufficiency.HEDGED:
        reasons.append("only individual reports support it")
    if NeedKind.CORROBORATION_HELPFUL in material:
        reasons.extend(
            need.reason for need in state.needs if need.kind == NeedKind.CORROBORATION_HELPFUL
        )
    if NeedKind.UPGRADE_AVAILABLE in material:
        reasons.append(
            "it rests on independent secondary sources; no original source was consulted"
        )
    if NeedKind.CONFLICT_MINOR in material:
        reasons.append("sources disagree on a detail")
    decisive_or_original = state.basis == Basis.DECISIVE or state.has_provenance_a
    if not reasons and decisive_or_original:
        return Confidence.HIGH, ["established by the original record or a decisive source"]
    if not reasons:
        reasons.append("it rests on independent secondary sources")
    return Confidence.MEDIUM, reasons


def _better(current: list, previous: list) -> bool:
    if not previous:
        return any(current[:5]) or bool(current and current[-1])
    if current[-1] > previous[-1]:
        # A newly found lead is progress: it opens a better route to the evidence.
        return True
    return current[:-1] > previous[:-1]


# --- evidence over time and contradictions ----------------------------------------------------


def mark_supersession(plan: ResearchPlan, accepted: list[EvidenceItem]) -> None:
    """On targets about the present, an older value gives way to a newer one for the same thing.

    Only items that state the same property of the same subjects under the same scope, each
    with its own date, can supersede one another. Historical targets keep every dated value.
    """
    groups: dict[tuple, list[tuple]] = defaultdict(list)
    for item in accepted:
        item.superseded_by = None
        target = plan.target(item.question_id)
        if target is None or target.profile.recency.kind != RecencyKind.CURRENT:
            continue
        stated = parse_date(item.as_of)
        if not item.attribute or stated is None:
            continue
        key = (
            item.question_id,
            tuple(sorted(item.entity_ids)),
            compact(item.attribute),
            compact(item.scope or ""),
        )
        groups[key].append((stated, item))
    for members in groups.values():
        if len(members) < 2:
            continue
        newest_date = max(stated for stated, _ in members)
        newest = next(item for stated, item in members if stated == newest_date)
        for stated, item in members:
            if stated < newest_date:
                item.superseded_by = newest.id


def classify_conflicts(
    groups: list[ContradictionGroup], accepted: list[EvidenceItem], resolve_allowance: int
) -> None:
    """Say why sources disagree when code can tell, and whether research should keep trying.

    A group links every item joined through any disagreeing pair, so it is explained pair by
    pair: each pair has its own cause (different dates, scopes, one superseding the other, or a
    shared upstream record), and the group is explained only when all of its pairs are. Each
    item then records its own unexplained opponents, which is what a claim citing it answers
    to; being in the same group as a disagreement is not being part of it.
    """
    by_id = {item.id: item for item in accepted}
    for item in accepted:
        item.contradicted_by = []
    for group in groups:
        members = [by_id[e] for e in group.evidence_ids if e in by_id]
        if len(members) < 2:
            continue
        pairs = [(by_id[a], by_id[b]) for a, b in group.pairs if a in by_id and b in by_id]
        if not group.pairs:
            _classify_by_members(group, members, resolve_allowance)
            for item in members:
                item.contradicted_by = None
            continue
        causes = [_pair_cause(left, right) for left, right in pairs]
        if group.status == ContradictionStatus.OPEN:
            if causes and all(causes):
                group.cause = max(set(causes), key=causes.count)  # type: ignore[type-var]
                group.status = ContradictionStatus.EXPLAINED
            elif group.intents_spent >= max(1, resolve_allowance):
                group.status = ContradictionStatus.REPRESENTED
        if group.status == ContradictionStatus.EXPLAINED:
            continue
        for (left, right), cause in zip(pairs, causes, strict=True):
            if cause is None:
                left.contradicted_by = [*(left.contradicted_by or []), right.id]
                right.contradicted_by = [*(right.contradicted_by or []), left.id]


def _pair_cause(left: EvidenceItem, right: EvidenceItem) -> ContradictionCause | None:
    """Why two items disagree, when code can tell; None when the disagreement is unexplained."""
    if left.superseded_by == right.id or right.superseded_by == left.id:
        return ContradictionCause.TIME
    if set(left.lead_ids) & set(right.lead_ids):
        return ContradictionCause.SHARED_UPSTREAM
    if left.scope and right.scope and compact(left.scope) != compact(right.scope):
        return ContradictionCause.SCOPE
    left_date, right_date = parse_date(left.as_of), parse_date(right.as_of)
    if left_date and right_date and left_date != right_date:
        return ContradictionCause.TIME
    return None


def _classify_by_members(
    group: ContradictionGroup, members: list[EvidenceItem], resolve_allowance: int
) -> None:
    """Groups recorded without their pairs are explained from their members as a whole."""
    if group.status != ContradictionStatus.OPEN:
        return
    scopes = {compact(m.scope) for m in members if m.scope}
    dates = {parse_date(m.as_of) for m in members if parse_date(m.as_of)}
    shared_leads = (
        set.intersection(*(set(m.lead_ids) for m in members))
        if all(m.lead_ids for m in members)
        else set()
    )
    superseded = {m.superseded_by for m in members if m.superseded_by} & {m.id for m in members}
    if superseded:
        # One of them is simply the newer statement of the other.
        group.cause, group.status = ContradictionCause.TIME, ContradictionStatus.EXPLAINED
    elif shared_leads:
        group.cause, group.status = (
            ContradictionCause.SHARED_UPSTREAM,
            ContradictionStatus.EXPLAINED,
        )
    elif len(scopes) > 1:
        group.cause, group.status = ContradictionCause.SCOPE, ContradictionStatus.EXPLAINED
    elif len(dates) > 1:
        group.cause, group.status = ContradictionCause.TIME, ContradictionStatus.EXPLAINED
    elif group.intents_spent >= max(1, resolve_allowance):
        group.status = ContradictionStatus.REPRESENTED
