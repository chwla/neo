"""Source strategy: which research action to take next, and how to search for it.

Actions come from what the evidence map says each target still lacks, never from a schedule.
A need is acted on only when it is material, pursued by the mode for that target's tier, and
has a viable intent: one whose attempt allowance is not spent, whose precondition holds, and
that has not stopped helping. The order is plain and testable: core before supporting, needs
that decide an answer before improvements to it, a lead already in hand before a new search.

Query wording comes from one model call per step, with a deterministic fallback built from the
target's own words. The planner's guesses shape at most a second attempt at finding an origin;
exact titles are searched only when evidence itself named them.
"""

from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from app.services.research.capability import items_per_call
from app.services.research.evidence_map import EvidenceMap
from app.services.research.llm_json import (
    LLMBudget,
    LLMBudgetExhausted,
    LLMFactory,
    ModelOutputInvalid,
    call_json,
)
from app.services.research.policy import ResearchPolicy
from app.services.research.textutil import jaccard, normalize_ws, token_set, tokens
from app.services.research.types import (
    Action,
    AuthorityKind,
    ContradictionGroup,
    ContradictionStatus,
    EvidenceTarget,
    Intent,
    LeadStatus,
    NeedKind,
    OriginatorKind,
    OriginHint,
    OriginLead,
    OriginStatus,
    Provenance,
    RecencyKind,
    RecordForm,
    ResearchPlan,
    ResearchTrace,
    TargetForm,
    TargetNeed,
    TargetState,
    Tier,
)

#: For each need, the intents that can resolve it, tried in order.
LADDERS: dict[NeedKind, list[Intent]] = {
    NeedKind.INSUFFICIENT: [
        Intent.FOLLOW_LEAD,
        Intent.DIRECT_ANSWER,
        # When plain questions have only turned up weak pages, the next thing to try is the
        # record itself, and then a readable copy of it, before asking the same thing wider.
        Intent.LOCATE_ORIGIN,
        Intent.ACCESSIBLE_COPY,
        Intent.BROADEN,
        Intent.CORROBORATE,
    ],
    NeedKind.SUBJECT_IMBALANCE: [],  # the lacking subject's own target acts on it
    NeedKind.CONFLICT_MATERIAL: [Intent.RESOLVE_CONFLICT],
    NeedKind.CONFLICT_MINOR: [Intent.RESOLVE_CONFLICT],
    NeedKind.STALE: [Intent.DIRECT_ANSWER],
    NeedKind.SCOPE_UNRESOLVED: [Intent.DIRECT_ANSWER],
    NeedKind.PROVENANCE_UNCONFIRMED: [Intent.FOLLOW_LEAD, Intent.LOCATE_ORIGIN],
    NeedKind.UPGRADE_AVAILABLE: [Intent.FOLLOW_LEAD, Intent.LOCATE_ORIGIN, Intent.ACCESSIBLE_COPY],
    NeedKind.CORROBORATION_HELPFUL: [Intent.CORROBORATE],
}
#: Needs in the order they are served: what decides an answer before what improves it. A
#: material conflict comes first because it is evidence in hand disagreeing about the answer:
#: until it is settled, more searching only adds to both sides.
SEVERITY: list[NeedKind] = [
    NeedKind.CONFLICT_MATERIAL,
    NeedKind.INSUFFICIENT,
    NeedKind.SUBJECT_IMBALANCE,
    NeedKind.STALE,
    NeedKind.SCOPE_UNRESOLVED,
    NeedKind.PROVENANCE_UNCONFIRMED,
    NeedKind.UPGRADE_AVAILABLE,
    NeedKind.CORROBORATION_HELPFUL,
    NeedKind.CONFLICT_MINOR,
]
_TIER_ORDER = {Tier.CORE: 0, Tier.SUPPORTING: 1, Tier.CONTEXT: 2}
#: Intents that improve an established answer by searching rather than by reading a lead.
_UPGRADE_SEARCHES = frozenset({Intent.LOCATE_ORIGIN, Intent.ACCESSIBLE_COPY})
#: Output one written query costs: an action id and a short search string.
_QUERY_OUTPUT_TOKENS = 40

QUERY_INSTRUCTIONS = """\
Write one web search query for each research action.

Each action gives the research question, its subjects, what kind of authority would settle it,
the form an original record would take, the intent, queries already tried, and any record the
evidence has already cited. Intents:
- direct_answer: find sources that state the answer.
- locate_origin: find the original record itself (the publication, filing, legal text,
  dataset, documentation, announcement or release notes that first established it). The action
  gives the kind of body that publishes such a record and the form it takes: word the query in
  those terms rather than repeating the question, and word a later attempt differently from an
  earlier one.
- follow_lead: find the cited record named in the action, by its title and issuer.
- accessible_copy: find another readable copy of the cited record named in the action.
- corroborate: find an independent source, not one of the sites already used.
- resolve_conflict: find sources that explain the disagreement described.
- broaden: use more general wording or related terms for the same question.

Queries are short keyword queries a person would type, name the subjects, include the scope
when one is given, and never repeat a tried query. Use a guessed issuer only when one is given.
Output: {"queries": [{"action_id": "...", "query": "..."}]}
"""


class _Query(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action_id: str = ""
    query: str = ""


class _QueryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    queries: list[_Query] = Field(default_factory=list)


# --- choosing actions ---------------------------------------------------------------------


def first_actions(emap: EvidenceMap, policy: ResearchPolicy) -> list[Action]:
    """Step 1: every core target, up to the mode's per-step limit.

    Per-subject and joint targets are researched through their per-subject children, and a
    joint target also gets a search for sources that compare its subjects directly. Whether an
    origin is searched for now is the planner's hint, and only now.
    """
    plan = emap.plan
    actions: list[Action] = []
    core = [t for t in plan.top_targets() if t.tier == Tier.CORE][
        : policy.ceilings.targets_per_step
    ]
    for target in core:
        work = plan.children(target.id) or [target]
        if target.form == TargetForm.JOINT and plan.children(target.id):
            work = [target, *work]
        for item in work:
            origin_now = (
                policy.step1_origin_search
                and item.profile.origin_hint == OriginHint.LIKELY
                and item.form == TargetForm.SINGLE
            )
            if policy.step1_actions_per_target <= 1:
                intents = [Intent.LOCATE_ORIGIN if origin_now else Intent.DIRECT_ANSWER]
            else:
                intents = [Intent.DIRECT_ANSWER, *([Intent.LOCATE_ORIGIN] if origin_now else [])]
            for intent in intents:
                actions.append(
                    _action(
                        item,
                        len(actions),
                        1,
                        intent,
                        NeedKind.INSUFFICIENT,
                        "nothing is known about this target yet",
                    )
                )
    return actions


def next_actions(
    emap: EvidenceMap,
    policy: ResearchPolicy,
    step: int,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
    capacity: int,
) -> list[Action]:
    """The most valuable viable actions, at most one per target and ``capacity`` targets."""
    plan = emap.plan
    order = {target.id: index for index, target in enumerate(plan.targets)}
    candidates = []
    for target in emap.pursued():
        if target.form == TargetForm.PER_SUBJECT and plan.children(target.id):
            continue  # its subjects act for it
        state = emap.state(target.id)
        choice = material_action(target, state, emap, policy, leads, groups)
        if choice is None:
            continue
        need, intent, lead = choice
        candidates.append(
            (
                _TIER_ORDER[target.tier],
                SEVERITY.index(need.kind),
                0 if intent == Intent.FOLLOW_LEAD else 1,
                sum(state.attempts.values()),
                order[target.id],
                target,
                need,
                intent,
                lead,
            )
        )
    candidates.sort(key=lambda row: row[:5])
    picked = _round_robin(candidates, plan, capacity)
    return [
        _action(
            target,
            index,
            step,
            intent,
            need.kind,
            need.reason,
            lead,
            attempt=emap.state(target.id).attempts.get(intent.value, 0) + 1,
        )
        for index, (*_, target, need, intent, lead) in enumerate(picked)
    ]


#: Provenance that means the pages answering a question vouch for nothing. Having found
#: nothing at all is a different state: then the question still needs asking.
_WEAK_SO_FAR = (Provenance.D, Provenance.C)


def _ladder_for(need: TargetNeed, state: TargetState) -> list[Intent]:
    """The intents that can resolve a need, in the order worth trying for this target.

    Asking the same question again is worth little once the answers coming back vouch for
    nothing: what changes the answer then is the record itself, so looking for it moves ahead
    of asking again. Once that record has been found and could not be read, a readable copy of
    it is worth more than searching for it a third time.
    """
    ladder = list(LADDERS[need.kind])
    if need.kind != NeedKind.INSUFFICIENT:
        return ladder
    weak = state.best_provenance in _WEAK_SO_FAR
    asked = state.attempts.get(Intent.DIRECT_ANSWER.value, 0)
    if weak and asked and Intent.LOCATE_ORIGIN in ladder:
        ladder.remove(Intent.LOCATE_ORIGIN)
        ladder.insert(ladder.index(Intent.DIRECT_ANSWER), Intent.LOCATE_ORIGIN)
    if state.origin_status == OriginStatus.LOCATED_UNREADABLE and Intent.ACCESSIBLE_COPY in ladder:
        ladder.remove(Intent.ACCESSIBLE_COPY)
        ladder.insert(ladder.index(Intent.LOCATE_ORIGIN), Intent.ACCESSIBLE_COPY)
    return ladder


def material_action(
    target: EvidenceTarget,
    state: TargetState,
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
) -> tuple[TargetNeed, Intent, OriginLead | None] | None:
    """The first need, by severity, that is material, pursued and has a viable intent."""
    for need in sorted(state.material_needs(), key=lambda n: SEVERITY.index(n.kind)):
        if target.tier not in policy.pursued_needs.get(need.kind, frozenset()):
            continue
        for intent in _ladder_for(need, state):
            viable, lead = _viable(target, state, need, intent, emap, policy, leads, groups)
            if viable:
                return need, intent, lead
    return None


def recovery_action(
    target: EvidenceTarget,
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
    step: int,
    index: int = 0,
) -> Action | None:
    """One more attempt at a target whose answer verification would not confirm.

    The evidence map may call the target established; what is missing is an answer that holds
    up, so this looks for evidence the way an unanswered target is looked for, within the same
    allowances.
    """
    state = emap.state(target.id)
    need = TargetNeed(
        kind=NeedKind.INSUFFICIENT,
        material=True,
        reason="verification could not confirm any answer to it",
    )
    for intent in _ladder_for(need, state):
        viable, lead = _viable(target, state, need, intent, emap, policy, leads, groups)
        if viable:
            return _action(
                target,
                index,
                step,
                intent,
                need.kind,
                need.reason,
                lead,
                attempt=state.attempts.get(intent.value, 0) + 1,
            )
    return None


def has_material_action(
    target: EvidenceTarget,
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
) -> bool:
    if target.tier not in policy.pursued_tiers:
        return False
    if target.form == TargetForm.PER_SUBJECT and emap.plan.children(target.id):
        return any(
            has_material_action(child, emap, policy, leads, groups)
            for child in emap.plan.children(target.id)
        )
    return material_action(target, emap.state(target.id), emap, policy, leads, groups) is not None


def _viable(
    target: EvidenceTarget,
    state: TargetState,
    need: TargetNeed,
    intent: Intent,
    emap: EvidenceMap,
    policy: ResearchPolicy,
    leads: list[OriginLead],
    groups: list[ContradictionGroup],
) -> tuple[bool, OriginLead | None]:
    used = state.attempts.get(intent.value, 0)
    if used >= policy.intent_allowance.get(intent, 0):
        return False, None
    if state.no_gain.get(intent.value, 0) >= policy.stall_window:
        return False, None
    family = emap.family(target)
    if intent == Intent.FOLLOW_LEAD:
        lead = next(
            (
                lead
                for lead in leads
                if lead.target_id in family
                and lead.status == LeadStatus.OPEN
                and (lead.href or lead.title)
            ),
            None,
        )
        return lead is not None, lead
    if intent in _UPGRADE_SEARCHES and need.kind == NeedKind.UPGRADE_AVAILABLE:
        if target.tier not in policy.upgrade_search_tiers:
            return False, None
    if intent == Intent.ACCESSIBLE_COPY:
        lead = next(
            (
                lead
                for lead in leads
                if lead.target_id in family and lead.status == LeadStatus.UNREADABLE and lead.title
            ),
            None,
        )
        return state.origin_status == OriginStatus.LOCATED_UNREADABLE and lead is not None, lead
    if intent == Intent.BROADEN and not state.attempts.get(Intent.DIRECT_ANSWER.value):
        return False, None
    if intent == Intent.CORROBORATE:
        if need.kind == NeedKind.INSUFFICIENT and not state.evidence_ids:
            return False, None
        # A record that was found but could not be read is still worth naming: another
        # authoritative source may report what it said.
        unreadable = next(
            (
                lead
                for lead in leads
                if lead.target_id in family and lead.status == LeadStatus.UNREADABLE and lead.title
            ),
            None,
        )
        return True, unreadable
    if intent == Intent.RESOLVE_CONFLICT:
        open_groups = [
            g for g in groups if g.question_id in family and g.status == ContradictionStatus.OPEN
        ]
        return bool(open_groups), None
    return True, None


def _round_robin(candidates: list, plan: ResearchPlan, capacity: int) -> list:
    """Spread the step across separable parts of the question so none starves another."""
    group_of: dict[str, int] = {}
    for index, group in enumerate(plan.complexity.separable_groups):
        for target_id in group:
            group_of[target_id] = index
    queues: dict[int, list] = defaultdict(list)
    for row in candidates:
        target = row[5]
        top = target.parent_id or target.id
        queues[group_of.get(top, 0)].append(row)
    picked: list = []
    while len(picked) < capacity and any(queues.values()):
        for key in sorted(queues):
            if queues[key] and len(picked) < capacity:
                picked.append(queues[key].pop(0))
    return picked


def _action(
    target: EvidenceTarget,
    index: int,
    step: int,
    intent: Intent,
    need: NeedKind,
    reason: str,
    lead: OriginLead | None = None,
    attempt: int = 1,
) -> Action:
    return Action(
        id=f"a{step}.{index + 1}",
        target_id=target.id,
        step=step,
        intent=intent,
        need=need,
        materiality_reason=reason,
        subject_ids=list(target.subject_ids),
        vertical=vertical_for(target, intent),
        time_filter=time_filter_for(target),
        lead_id=lead.id if lead else None,
        attempt=attempt,
    )


#: Bodies whose records are published as literature rather than on the open web.
_SCHOLARLY_ORIGINATORS = frozenset(
    {OriginatorKind.ACADEMIC_INSTITUTION, OriginatorKind.RESEARCH_AUTHORS}
)


def vertical_for(target: EvidenceTarget, intent: Intent) -> str:
    """The kind of search that suits the evidence a target needs. Never its topic."""
    profile = target.profile
    if intent in (Intent.DIRECT_ANSWER, Intent.LOCATE_ORIGIN, Intent.CORROBORATE):
        if (
            profile.authority_kind == AuthorityKind.CONSENSUS
            or profile.record_form == RecordForm.PUBLICATION
            or profile.originator in _SCHOLARLY_ORIGINATORS
        ):
            return "scholarly"
        if profile.originator == OriginatorKind.NEWS_ORGANISATION:
            return "news"
        if profile.recency.kind == RecencyKind.AS_OF_EVENT or (
            profile.recency.kind == RecencyKind.CURRENT
            and profile.record_form == RecordForm.ANNOUNCEMENT
        ):
            return "news"
    return "web"


def time_filter_for(target: EvidenceTarget) -> str | None:
    recency = target.profile.recency
    if recency.kind != RecencyKind.CURRENT or not recency.max_age_days:
        return None
    if recency.max_age_days <= 31:
        return "month"
    if recency.max_age_days <= 366:
        return "year"
    return None


# --- writing queries ------------------------------------------------------------------------


def write_queries(
    actions: list[Action],
    plan: ResearchPlan,
    emap: EvidenceMap,
    leads: list[OriginLead],
    *,
    tried: dict[str, list[str]],
    used_hosts: list[str],
    llm_factory: LLMFactory,
    budget: LLMBudget,
    trace: ResearchTrace,
    reserved: bool = False,
) -> None:
    """Fill in each action's query: one model call for the step, code where it cannot help."""
    by_lead = {lead.id: lead for lead in leads}
    needing = [
        a for a in actions if not (a.intent == Intent.FOLLOW_LEAD and _lead_href(a, by_lead))
    ]
    written: dict[str, str] = {}
    per_call = items_per_call(
        budget.capability,
        output_tokens_per_item=_QUERY_OUTPUT_TOKENS,
        overhead_tokens=80,
        ceiling=max(1, len(needing)),
    )
    for start in range(0, len(needing), per_call):
        batch = needing[start : start + per_call]
        if not batch or not budget.can_spend(reserved=reserved):
            break
        payload = {
            "actions": [
                _action_payload(a, plan, emap, by_lead, tried.get(a.target_id, [])) for a in batch
            ],
            "sites_already_used": used_hosts[:15],
        }
        try:
            output = call_json(
                llm_factory,
                stage="research_queries",
                instructions=QUERY_INSTRUCTIONS,
                payload=payload,
                schema=_QueryOutput,
                budget=budget,
                num_predict=900,
                notes=trace.errors,
                reserved=reserved,
            )
        except (ModelOutputInvalid, LLMBudgetExhausted) as exc:
            trace.errors.append(f"query writing fell back to the plan's own words: {exc}")
            continue
        for item in output.queries:
            query = normalize_ws(item.query)[:160]
            if item.action_id and token_set(query):
                written.setdefault(item.action_id, query)
    for action in actions:
        if action.intent == Intent.FOLLOW_LEAD and _lead_href(action, by_lead):
            action.query = ""
            continue
        candidate = written.get(action.id, "")
        previous = tried.get(action.target_id, [])
        if not candidate or _repeats(candidate, previous):
            candidate = fallback_query(action, plan, by_lead)
        action.query = candidate


def fallback_query(action: Action, plan: ResearchPlan, by_lead: dict[str, OriginLead]) -> str:
    """A query from the target's own words: its subjects, key terms and the form of its record."""
    target = plan.target(action.target_id)
    names = [entity.name for sid in action.subject_ids if (entity := plan.entity(sid))]
    name_tokens = {token for name in names for token in tokens(name)}
    key_terms = [t for t in dict.fromkeys(tokens(target.text)) if t not in name_tokens][:6]
    lead = by_lead.get(action.lead_id or "")
    parts: list[str] = [*names[:3]]
    if action.intent in (Intent.FOLLOW_LEAD, Intent.ACCESSIBLE_COPY) and lead is not None:
        parts = [lead.title or " ".join(key_terms), lead.issuer]
    elif action.intent == Intent.CORROBORATE and lead is not None and lead.title:
        # Somebody else reporting what the unreadable record said is evidence of it.
        parts = [*names[:2], lead.title]
    elif action.intent == Intent.BROADEN:
        dimension = plan.dimension(target.dimension_id)
        parts.append(dimension.name if dimension else " ".join(key_terms[:3]))
    else:
        parts.append(" ".join(key_terms))
        if action.intent == Intent.LOCATE_ORIGIN:
            parts.extend(origin_terms(target, action.attempt))
    if target.scope_note and action.intent != Intent.FOLLOW_LEAD:
        parts.append(target.scope_note)
    return normalize_ws(" ".join(part for part in parts if part))[:160]


def origin_terms(target: EvidenceTarget, attempt: int) -> list[str]:
    """Words that describe the record being looked for, escalating with each attempt.

    A record is described by what it is and who publishes it, both of which the plan already
    says in its own vocabulary. Repeating one wording finds the same pages, so a later attempt
    asks in the other's terms: the form of the document first, then the kind of body behind it.
    """
    profile = target.profile
    form = (
        profile.record_form.value.replace("_", " ")
        if profile.record_form != RecordForm.OTHER
        else ""
    )
    body = (
        profile.originator.value.replace("_", " ")
        if profile.originator != OriginatorKind.OTHER
        else ""
    )
    available = [term for term in (form, body) if term]
    if not available:
        return []
    # One way of describing it per attempt, taking the next way each time.
    return [available[(max(1, attempt) - 1) % len(available)]]


def _action_payload(
    action: Action,
    plan: ResearchPlan,
    emap: EvidenceMap,
    by_lead: dict[str, OriginLead],
    tried: list[str],
) -> dict:
    target = plan.target(action.target_id)
    payload = {
        "action_id": action.id,
        "intent": action.intent.value,
        "attempt": action.attempt,
        "question": target.text,
        "scope": target.scope_note,
        "subjects": [
            {"name": entity.name, "aliases": entity.aliases}
            for sid in action.subject_ids
            if (entity := plan.entity(sid))
        ],
        "authority_kind": target.profile.authority_kind.value,
        "record_form": target.profile.record_form.value,
        "published_by": target.profile.originator.value,
        "tried_queries": tried[-6:],
    }
    if action.intent in (Intent.LOCATE_ORIGIN, Intent.ACCESSIBLE_COPY):
        payload["record_terms"] = origin_terms(target, action.attempt)
    lead = by_lead.get(action.lead_id or "")
    if lead is not None:
        payload["cited_record"] = {"title": lead.title, "issuer": lead.issuer, "year": lead.year}
    state = emap.state(target.id)
    # The planner's guess about who issued the record shapes only a second attempt to find it.
    if (
        action.intent == Intent.LOCATE_ORIGIN
        and state.attempts.get(Intent.LOCATE_ORIGIN.value, 0) >= 1
        and target.hypotheses.likely_issuers
    ):
        payload["guessed_issuer"] = target.hypotheses.likely_issuers[0]
    if action.intent == Intent.RESOLVE_CONFLICT:
        payload["disagreement"] = next(
            (need.reason for need in state.needs if need.kind.value.startswith("conflict")), ""
        )
    return payload


def _lead_href(action: Action, by_lead: dict[str, OriginLead]) -> str | None:
    lead = by_lead.get(action.lead_id or "")
    return lead.href if lead is not None else None


def _repeats(query: str, previous: list[str]) -> bool:
    wanted = token_set(query)
    return any(jaccard(wanted, token_set(old)) >= 0.8 for old in previous)
