"""Research planning: what the research has to establish, and what evidence would establish it.

The planner turns a query into evidence targets. Each target is a neutral question the
research must answer, with a profile of the evidence that would settle it: what kind of
authority, what the claim is about, whether an originating record probably exists and what
form it would take, how recent evidence must be. Those are hints for choosing searches; what
research later observes always outranks them.

One model call proposes the plan when the routed model can answer at that length. A model
with a smaller answer limit is asked in stages: the task, the dimensions that matter, then the
targets those raise. The proposal is checked by code that knows nothing about the topic but a
lot about what a coherent plan looks like. Problems code can fix are repaired in place;
problems only the model can fix trigger one repair call.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Any, ClassVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.services.research.capability import ModelCapability, items_per_call
from app.services.research.llm_json import (
    LLMBudget,
    LLMBudgetExhausted,
    LLMFactory,
    ModelOutputInvalid,
    call_json,
)
from app.services.research.policy import POLICIES
from app.services.research.textutil import (
    compact,
    jaccard,
    mentions_any,
    name_forms,
    normalize_ws,
    token_set,
)
from app.services.research.types import (
    AuthorityKind,
    ClaimScope,
    Constraint,
    DecisionCriterion,
    DepthMode,
    Dimension,
    Entity,
    ErrorProne,
    EvidenceProfile,
    EvidenceTarget,
    OriginatorKind,
    OriginHint,
    OutcomeRole,
    PlanComplexity,
    PlanIssue,
    Recency,
    RecencyKind,
    RecordForm,
    ResearchDecomposition,
    ResearchPlan,
    ResearchTrace,
    TargetForm,
    TargetHypotheses,
    TaskType,
    TaskUnderstanding,
    Tier,
    TimeSensitivity,
)


class PlanningFailed(RuntimeError):
    """The planner could not produce a usable plan."""

    def __init__(self, message: str, issues: list[PlanIssue] | None = None) -> None:
        super().__init__(message)
        #: What validation found in the last plan proposed, so a failed run can show why.
        self.issues = issues or []


# A target that refers to the research itself rather than to the subject.
_META_REFERENCE = re.compile(
    r"\b(?:this|the user'?s?|the original)\s+(?:query|question|request|prompt|research|report)\b"
    r"|\b(?:reliable|credible|trustworthy|available)\s+sources?\b",
    re.IGNORECASE,
)
_REPAIRABLE_BY_MODEL = frozenset(
    {
        "constraint_unrepresented",
        "criterion_unrepresented",
        "decision_criteria_missing",
        "comparison_needs_entities",
        "entity_unresearched",
        "no_core_targets",
        "target_without_dimension",
        "target_presupposes_answer",
    }
)
#: Notes recording a part of the proposal that code discarded as invalid. A repair is told
#: them with its problems: a plan that lost its targets is fixed by what discarded them.
_DISCARDS = frozenset(
    {
        "constraint_not_in_query",
        "dimension_unjustified",
        "meta_target",
        "target_without_dimension",
    }
)

#: Output one planned target costs: its text, profile, hypotheses and a search or two.
_TARGET_OUTPUT_TOKENS = 300
#: Framing every plan answer carries, whatever it contains.
_PLAN_OVERHEAD_TOKENS = 400
#: Days evidence about the present may be old when the planner gives no limit.
_DEFAULT_CURRENT_AGE_DAYS = 365

_PROFILE_FIELDS = """\
  - text: a neutral question answerable from sources. Never state or assume the answer in the
    question; write what you expect the answer to be in hypotheses.expected_answer instead.
    Never ask about the research process, about which sources exist, or about the query itself.
  - dimension_id: the id of the dimension the target belongs to. Every target has one.
  - tier: core when the final answer depends on it; supporting when it adds detail the answer
    needs; context only for background the question explicitly benefits from.
  - form: single (about one subject or none), per_subject (the same question asked of each
    listed subject), joint (answerable only by comparing or relating the listed subjects).
  - subject_ids: the entities the target is about.
  - scope_note: the version, period, population or place the target means when a name or term
    could refer to several; otherwise empty.
  - authority_kind: issuer (what an organisation, person or project itself said, did,
    specified, published or released), measurement (a figure from data or statistics),
    consensus (what research or expert bodies have established overall), evaluative (how good,
    reliable or suitable something is, as judged by others).
  - claim_scope: record (what a specific document, issuer or study states or reports),
    instance (a specific event, case or individual report), general (a generalisation about a
    population, product, practice or trend).
  - origin_hint: likely when an original record that settles the target probably exists
    publicly (a paper, filing, statute, specification, release note, dataset or official
    announcement); unlikely when it probably does not; unknown otherwise.
  - record_form: publication, filing, legal_text, dataset, documentation, announcement,
    release_notes or other: the form that original record would take.
  - originator: the kind of body that would have published that record: government_agency,
    city_authority, regulator, statistical_office, operating_authority, standards_body,
    court_or_legislature, academic_institution, research_authors, company_publisher,
    project_maintainers, news_organisation, or other. Answer for the kind of body, whoever
    it happens to be: this is how the search for the original record is worded.
  - outcome_role: benefit when the target asks whether the thing achieved the effect the query
    is about; harm when it asks whether it caused a harm, cost or side effect the query asks
    about; neither for an ordinary question of fact. A query that asks whether something
    worked without causing harm has both, and both are core.
  - recency: timeless (settled), as_of_event (about a specific time or event) or current
    (depends on the present state). max_age_days: for current targets, how old evidence may be.
  - error_prone: estimate, forecast or self_reported when even an authoritative source can
    only give that; otherwise none.
  - hypotheses: {likely_issuers, record_title_words, expected_answer}: who probably published
    the original record, words probably in its title, and what you currently believe the
    answer is. These are guesses used only to word searches; they are never evidence.
  - constraint_ids / criterion_ids: which constraints and criteria the target serves."""

TASK_INSTRUCTIONS = """\
Work out what the user's query is asking for. Describe only the task, not how to research it.

Fields:
- task_type: factual_lookup, explanation, comparison, recommendation,
  technical_investigation, product_evaluation, scientific_research, current_events,
  historical_research, troubleshooting, market_research, entity_research or open_exploration.
- answer_shape: a short phrase describing what a useful answer looks like.
- time_sensitivity: high when the answer depends on the current state of things (prices,
  versions, availability, office holders, laws in force, recent events); medium when it
  changes over years; low when it is settled.
- contested_likely: true when credible sources are likely to disagree on the core answer.
- entities: [{id, name, type, aliases}] the specific things the query is about, named or
  clearly implied. type is a short free-text description. aliases are other names sources use.
- constraints: [{id, text}] conditions the user explicitly stated (use case, audience,
  location, budget, timeframe, priorities), in the query's own words.
- decision_criteria: [{id, text}] only when the user has to choose or act: the criteria the
  choice should actually turn on for this user.

Use ids like e1, k1, d1.
Output: {"task_type", "answer_shape", "time_sensitivity", "contested_likely", "entities",
"constraints", "decision_criteria"}
"""

DIMENSION_INSTRUCTIONS = """\
Name the aspects whose answers would change the answer to this query.

- dimensions: [{id, name, description, justification_refs}] justification_refs lists "query"
  and/or the constraint and criterion ids that make the aspect matter. Never include an
  aspect only because it is commonly discussed for this kind of subject. Keep dimensions
  distinct from one another.

Respect the limit in the INPUT. Use ids like m1.
Output: {"dimensions": [...]}
"""

TARGET_INSTRUCTIONS = (
    """\
Write the next evidence targets for this research plan: what the research has to establish.

- targets: [{id, text, dimension_id, tier, form, subject_ids, scope_note, authority_kind,
  claim_scope, origin_hint, record_form, recency, max_age_days, error_prone, hypotheses,
  constraint_ids, criterion_ids}]
"""
    + _PROFILE_FIELDS
    + """

Use as few targets as the query needs; the limits in the INPUT are maximums, not goals. Do not
repeat a target already listed in the INPUT. Every constraint and criterion in the INPUT must
end up served by some target.
Output: {"targets": [...]}
"""
)

PLAN_INSTRUCTIONS = (
    """\
You design research plans. Work out what must be established to answer the user's query
well: the targets the research must answer and what evidence would settle each.

Fields:
- task_type: factual_lookup, explanation, comparison, recommendation,
  technical_investigation, product_evaluation, scientific_research, current_events,
  historical_research, troubleshooting, market_research, entity_research or open_exploration.
- answer_shape: a short phrase describing what a useful answer looks like.
- time_sensitivity: high, medium or low: how much the answer depends on the current state.
- contested_likely: true when credible sources are likely to disagree on the core answer.
- entities: [{id, name, type, aliases}] the specific things the query is about, named or
  clearly implied. type is a short free-text description. aliases are other names sources use.
- constraints: [{id, text}] conditions the user explicitly stated, in the query's own words.
- decision_criteria: [{id, text}] only when the user has to choose or act.
- dimensions: [{id, name, description, justification_refs}] aspects whose answers change the
  answer to this query. justification_refs lists "query" and/or the constraint and criterion
  ids that make the aspect matter. Never include an aspect only because it is commonly
  discussed for this kind of subject. Keep dimensions distinct.
- targets: [{id, text, dimension_id, tier, form, subject_ids, scope_note, authority_kind,
  claim_scope, origin_hint, record_form, recency, max_age_days, error_prone, hypotheses,
  constraint_ids, criterion_ids}]
"""
    + _PROFILE_FIELDS
    + """

Use as few targets as the query needs; the limits in the INPUT are maximums, not goals. A
simple factual question needs one target. Follow the decomposition guidance in the INPUT.

When a query asks about something across several cases -- places, products, versions,
populations, periods or organisations -- name the cases you can as entities and ask the
question of each of them, then add the general target with claim_scope general. What happened
in one case is evidence about that case; a statement about all of them needs its own target.
Use ids like e1, k1, d1, m1, t1.
"""
)

REPAIR_INSTRUCTIONS = (
    PLAN_INSTRUCTIONS
    + "\nThe INPUT contains a previous plan and the problems found in it. Return a complete "
    "corrected plan with the same fields that resolves every problem."
)


def _texts(value: Any) -> list[str]:
    """Every text a value holds: itself when it is a single value, else its items' or values'."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, dict):
        value = list(value.values())
    return [text for item in value for text in _texts(item)] if isinstance(value, list) else []


def _as_refs(value: Any) -> list[str]:
    """References to things the plan contains.

    A model may write a reference as an object under keys of its own choosing
    ({"source": "query", "id": "d1"}) rather than as the bare id. Every value the object holds
    is offered and validation keeps those naming something in the plan, so the reference is
    neither lost nor read as the object's printed form, which would match nothing.
    """
    return list(dict.fromkeys(_texts(value)))


def _as_names(value: Any) -> list[str]:
    """Free-text names, where an object counts only when it holds a single text.

    Nothing filters names afterwards, and a stray value kept as a name would make unrelated
    text look like it names the entity.
    """
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in items:
        texts = _texts(item)
        if len(texts) == 1 or not isinstance(item, (dict, list)):
            names.extend(texts)
    return names


RefList = Annotated[list[str], BeforeValidator(_as_refs)]
NameList = Annotated[list[str], BeforeValidator(_as_names)]


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)
    #: The field an item written as a bare string stands for ("entities": ["A", "B"]).
    bare_text_field: ClassVar[str | None] = None

    @model_validator(mode="before")
    @classmethod
    def _bare_text(cls, data: Any) -> Any:
        if isinstance(data, str) and cls.bare_text_field:
            return {cls.bare_text_field: data}
        return data


class DraftEntity(_Lenient):
    bare_text_field = "name"
    id: str = ""
    name: str = ""
    type: str = ""
    aliases: NameList = Field(default_factory=list)


class DraftRef(_Lenient):
    bare_text_field = "text"
    id: str = ""
    text: str = ""


class DraftDimension(_Lenient):
    bare_text_field = "name"
    id: str = ""
    name: str = ""
    description: str = ""
    justification_refs: RefList = Field(default_factory=list)


class DraftHypotheses(_Lenient):
    bare_text_field = "expected_answer"
    likely_issuers: NameList = Field(default_factory=list)
    record_title_words: NameList = Field(default_factory=list)
    expected_answer: str = ""


class DraftTarget(_Lenient):
    bare_text_field = "text"
    id: str = ""
    text: str = ""
    dimension_id: str = ""
    tier: str = "supporting"
    form: str = ""
    subject_ids: RefList = Field(default_factory=list)
    scope_note: str = ""
    authority_kind: str = ""
    claim_scope: str = ""
    origin_hint: str = ""
    record_form: str = ""
    originator: str = ""
    outcome_role: str = ""
    recency: str = ""
    max_age_days: str | None = None
    error_prone: str = ""
    hypotheses: DraftHypotheses = Field(default_factory=DraftHypotheses)
    constraint_ids: RefList = Field(default_factory=list)
    criterion_ids: RefList = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _older_names(cls, data: Any) -> Any:
        # Models sometimes answer with the question-shaped field names they know better.
        if isinstance(data, dict):
            data = dict(data)
            if "subject_ids" not in data and "entity_ids" in data:
                data["subject_ids"] = data["entity_ids"]
            if "tier" not in data and "importance" in data:
                data["tier"] = data["importance"]
        return data


class UnderstandingDraft(_Lenient):
    task_type: str = "open_exploration"
    answer_shape: str = ""
    time_sensitivity: str = "medium"
    contested_likely: bool = False
    entities: list[DraftEntity] = Field(default_factory=list)
    constraints: list[DraftRef] = Field(default_factory=list)
    decision_criteria: list[DraftRef] = Field(default_factory=list)


class DimensionsDraft(_Lenient):
    dimensions: list[DraftDimension] = Field(default_factory=list)


class TargetsDraft(_Lenient):
    targets: list[DraftTarget] = Field(default_factory=list)


class PlanDraft(_Lenient):
    task_type: str = "open_exploration"
    answer_shape: str = ""
    time_sensitivity: str = "medium"
    contested_likely: bool = False
    entities: list[DraftEntity] = Field(default_factory=list)
    constraints: list[DraftRef] = Field(default_factory=list)
    decision_criteria: list[DraftRef] = Field(default_factory=list)
    dimensions: list[DraftDimension] = Field(default_factory=list)
    targets: list[DraftTarget] = Field(default_factory=list)


def _limits(depth: DepthMode) -> dict[str, int]:
    caps = POLICIES[depth].planning
    return {
        "max_core_targets": caps.core,
        "max_supporting_targets": caps.supporting,
        "max_context_targets": caps.context,
    }


def _target_cap(depth: DepthMode) -> int:
    caps = POLICIES[depth].planning
    return caps.core + caps.supporting + caps.context


def plan_research(
    query: str,
    depth: DepthMode,
    *,
    llm_factory: LLMFactory,
    budget: LLMBudget,
    context: str = "",
) -> ResearchPlan:
    # The mode caps how many targets the plan may hold. A model that cannot answer at that
    # length is asked for them in stages, not given a smaller plan.
    max_targets = _target_cap(depth)
    payload: dict[str, Any] = {
        "query": query,
        "limits": _limits(depth),
        "decomposition_guidance": POLICIES[depth].planning.guidance,
    }
    if context.strip():
        payload["user_context"] = {
            "note": "Background about the user. It may inform priorities; it is not evidence.",
            "text": context[:2000],
        }
    staged = not _whole_plan_fits(budget.capability, max_targets)
    try:
        if staged:
            draft = _staged_draft(llm_factory, budget, payload, max_targets)
        else:
            draft = call_json(
                llm_factory,
                stage="research_plan",
                instructions=PLAN_INSTRUCTIONS,
                payload=payload,
                schema=PlanDraft,
                budget=budget,
                num_predict=4000,
            )
    except (ModelOutputInvalid, LLMBudgetExhausted) as exc:
        raise PlanningFailed(f"The research planner did not return a usable plan ({exc}).") from exc

    plan = validate_plan(draft, query, depth)
    plan_draft = draft
    # What validation found in the proposal the plan stands on, without earlier attempts'.
    proposal_issues = plan.issues
    unresolved = [
        issue for issue in plan.issues if not issue.repaired and issue.code in _REPAIRABLE_BY_MODEL
    ]
    # A model that could not hold a whole plan cannot hold a whole repaired plan either.
    if unresolved and not staged and budget.can_spend():
        try:
            repaired_draft = call_json(
                llm_factory,
                stage="research_plan_repair",
                instructions=REPAIR_INSTRUCTIONS,
                payload={
                    **payload,
                    "previous_plan": draft.model_dump(),
                    "problems": [
                        f"{issue.code}: {issue.detail}"
                        for issue in plan.issues
                        if issue in unresolved or issue.code in _DISCARDS
                    ],
                },
                schema=PlanDraft,
                budget=budget,
                num_predict=4000,
            )
            candidate = validate_plan(repaired_draft, query, depth)
            if _plan_score(candidate) >= _plan_score(plan):
                proposal_issues = candidate.issues
                candidate.issues = [
                    *[issue.model_copy(update={"repaired": False}) for issue in unresolved],
                    PlanIssue(code="model_repair_applied", repaired=True),
                    *candidate.issues,
                ]
                plan = candidate
                plan_draft = repaired_draft
        except (ModelOutputInvalid, LLMBudgetExhausted):
            plan.issues.append(PlanIssue(code="model_repair_failed"))
    if not any(t.tier == Tier.CORE for t in plan.top_targets()):
        # A dimension left unjustified only by its references -- missing, or naming nothing the
        # plan holds -- would otherwise take every core target in it down with it.
        adopted = validate_plan(plan_draft, query, depth, adopt_unjustified=True)
        if any(t.tier == Tier.CORE for t in adopted.top_targets()):
            # Keep what the run itself recorded (a repair's outcome), not the proposal's notes
            # twice: the adopted plan re-validated the same proposal.
            proposal = {id(issue) for issue in proposal_issues}
            adopted.issues = [
                *[issue for issue in plan.issues if id(issue) not in proposal],
                *adopted.issues,
            ]
            plan = adopted
    if not any(t.tier == Tier.CORE for t in plan.top_targets()):
        discarded = Counter(issue.code for issue in proposal_issues if issue.code in _DISCARDS)
        reasons = ", ".join(f"{n} {code.replace('_', ' ')}" for code, n in discarded.items())
        raise PlanningFailed(
            "The research planner produced no usable core research targets"
            + (f" (discarded: {reasons})." if reasons else "."),
            plan.issues,
        )
    return plan


def _whole_plan_fits(capability: ModelCapability, max_targets: int) -> bool:
    """Whether the routed model can answer with a whole plan rather than a cut-off one."""
    needed = _PLAN_OVERHEAD_TOKENS + max(2, max_targets) * _TARGET_OUTPUT_TOKENS
    return capability.max_output_tokens >= needed


def _targets_per_call(capability: ModelCapability, max_targets: int) -> int:
    return max(
        1,
        items_per_call(
            capability,
            output_tokens_per_item=_TARGET_OUTPUT_TOKENS,
            overhead_tokens=120,
            ceiling=max_targets,
        ),
    )


def planning_call_budget(depth: DepthMode, capability: ModelCapability) -> int:
    """Model calls a plan needs on this model: one answer, or several smaller ones.

    Callers that plan outside a research run (the plan preview) size their budget with this,
    so a model that has to be asked in stages is given the calls those stages take.
    """
    targets = _target_cap(depth)
    if _whole_plan_fits(capability, targets):
        return 2
    batches = -(-targets // _targets_per_call(capability, targets))
    # The task, its dimensions, a call per batch of targets, and one spare.
    return 3 + batches


def _staged_draft(
    llm_factory: LLMFactory, budget: LLMBudget, payload: dict[str, Any], max_targets: int
) -> PlanDraft:
    """Ask a small-answer model for the plan a piece at a time, then assemble it.

    The pieces are the same plan: what the task is, which aspects matter, and the targets
    those aspects raise.
    """
    understanding = call_json(
        llm_factory,
        stage="research_task",
        instructions=TASK_INSTRUCTIONS,
        payload={key: payload[key] for key in ("query", "user_context") if key in payload},
        schema=UnderstandingDraft,
        budget=budget,
        num_predict=1200,
    )
    context = {
        "query": payload["query"],
        "task_type": understanding.task_type,
        "answer_shape": understanding.answer_shape,
        "entities": [item.model_dump() for item in understanding.entities],
        "constraints": [item.model_dump() for item in understanding.constraints],
        "decision_criteria": [item.model_dump() for item in understanding.decision_criteria],
    }
    dimension_limit = max(2, min(5, max_targets))
    dimensions: list[DraftDimension] = []
    if budget.can_spend():
        try:
            dimensions = call_json(
                llm_factory,
                stage="research_dimensions",
                instructions=DIMENSION_INSTRUCTIONS,
                payload={**context, "limits": {"max_dimensions": dimension_limit}},
                schema=DimensionsDraft,
                budget=budget,
                num_predict=900,
            ).dimensions
        except (ModelOutputInvalid, LLMBudgetExhausted):
            dimensions = []

    per_call = _targets_per_call(budget.capability, max_targets)
    targets: list[DraftTarget] = []
    while len(targets) < max_targets and budget.can_spend():
        wanted = min(per_call, max_targets - len(targets))
        try:
            batch = call_json(
                llm_factory,
                stage="research_targets",
                instructions=TARGET_INSTRUCTIONS,
                payload={
                    **context,
                    "dimensions": [item.model_dump() for item in dimensions],
                    "targets_so_far": [item.text for item in targets],
                    "limits": {**payload["limits"], "max_targets_in_this_answer": wanted},
                    "decomposition_guidance": payload["decomposition_guidance"],
                },
                schema=TargetsDraft,
                budget=budget,
                num_predict=2000,
            ).targets
        except (ModelOutputInvalid, LLMBudgetExhausted):
            break
        fresh = [item for item in batch if item.text and item.text not in {t.text for t in targets}]
        if not fresh:
            break
        targets.extend(fresh)
        # A batch that came back short says the plan is complete, not that more should follow.
        if len(fresh) < wanted:
            break
    return PlanDraft(
        task_type=understanding.task_type,
        answer_shape=understanding.answer_shape,
        time_sensitivity=understanding.time_sensitivity,
        contested_likely=understanding.contested_likely,
        entities=understanding.entities,
        constraints=understanding.constraints,
        decision_criteria=understanding.decision_criteria,
        dimensions=dimensions,
        targets=targets,
    )


def _plan_score(plan: ResearchPlan) -> tuple[int, int]:
    unresolved = sum(
        1 for issue in plan.issues if not issue.repaired and issue.code in _REPAIRABLE_BY_MODEL
    )
    return (-unresolved, sum(1 for t in plan.top_targets() if t.tier == Tier.CORE))


def validate_plan(
    draft: PlanDraft, query: str, depth: DepthMode, *, adopt_unjustified: bool = False
) -> ResearchPlan:
    """Check and repair a proposed plan. Pure: no model calls, no topic knowledge.

    ``adopt_unjustified`` keeps an unjustified dimension that a core target belongs to, rather
    than discarding it and every target in it. It is the last resort for a plan that would
    otherwise have no core target, once the model has had whatever chance it gets to repair.
    """
    issues: list[PlanIssue] = []

    def note(code: str, detail: str = "", repaired: bool = True) -> None:
        issues.append(PlanIssue(code=code, detail=normalize_ws(detail)[:300], repaired=repaired))

    task_type = _coerce(TaskType, draft.task_type, TaskType.OPEN_EXPLORATION, note, "task_type")
    sensitivity = _coerce(
        TimeSensitivity, draft.time_sensitivity, TimeSensitivity.MEDIUM, note, "time_sensitivity"
    )

    entities = _entities(draft, note)
    entity_lookup = _entity_lookup(entities)
    constraints = _constraints(draft, query, note)
    criteria = [
        DecisionCriterion(
            id=_fresh_id(raw.id, "d", index, set()), text=normalize_ws(raw.text)[:200]
        )
        for index, raw in enumerate(draft.decision_criteria, 1)
        if normalize_ws(raw.text)
    ]
    criteria = _unique_ids(criteria, "d")

    refs = _ref_lookup(constraints, criteria)
    adopt: frozenset[str] = frozenset()
    if adopt_unjustified:
        # The targets that will be core: those marked so, or the first few when none is,
        # as the promotion below would make them.
        core = [raw for raw in draft.targets if compact(raw.tier or "") == Tier.CORE.value]
        adopt = frozenset(
            compact(raw.dimension_id) for raw in core or draft.targets[:3] if raw.dimension_id
        )
    dimensions, dimension_remap = _dimensions(draft, refs, note, adopt)

    targets = _targets(
        draft,
        query,
        dimensions,
        dimension_remap,
        entities,
        entity_lookup,
        refs,
        constraints,
        criteria,
        note,
    )

    if task_type == TaskType.RECOMMENDATION and not criteria:
        note("decision_criteria_missing", "a decision task needs explicit decision criteria", False)
    if task_type == TaskType.COMPARISON and len(entities) < 2:
        note("comparison_needs_entities", "a comparison needs at least two entities", False)

    if targets and not any(t.tier == Tier.CORE for t in targets):
        for target in targets[:3]:
            target.tier = Tier.CORE
        note("core_targets_promoted", "no target was marked core")
    _pair_outcomes(targets, note)
    if not targets:
        note("no_core_targets", "no usable research targets", False)
    targets = _clamp_tiers(targets, depth, note)

    for criterion in criteria:
        if not any(criterion.id in t.criterion_ids for t in targets if t.tier == Tier.CORE):
            note("criterion_unrepresented", criterion.text, False)
    for constraint in constraints:
        served = any(constraint.id in t.constraint_ids for t in targets) or any(
            constraint.id in d.justification_refs for d in dimensions
        )
        if not served:
            note("constraint_unrepresented", constraint.text, False)

    used_dimensions = {t.dimension_id for t in targets}
    for dimension in dimensions:
        if dimension.id not in used_dimensions:
            note("dimension_unreferenced", dimension.name)
    dimensions = [d for d in dimensions if d.id in used_dimensions]

    researched = {entity_id for t in targets for entity_id in t.subject_ids}
    for entity in entities:
        if entity.id not in researched:
            important = task_type in (TaskType.COMPARISON, TaskType.RECOMMENDATION)
            note("entity_unresearched", entity.name, not important)
    entities = [e for e in entities if e.id in researched]

    plan = ResearchPlan(
        query=query,
        depth=depth,
        understanding=TaskUnderstanding(
            task_type=task_type,
            answer_shape=normalize_ws(draft.answer_shape)[:200],
            time_sensitivity=sensitivity,
            entities=entities,
            constraints=constraints,
            decision_criteria=criteria,
        ),
        decomposition=ResearchDecomposition(dimensions=dimensions),
        targets=[*targets, *_children(targets, entities)],
        issues=issues,
    )
    plan.complexity = _complexity(plan, bool(draft.contested_likely))
    plan.issues = issues
    return plan


# --- validation helpers ----------------------------------------------------------------


def _coerce(enum_type, value: str | None, default, note, field_name: str):
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError:
        if value:
            note(f"invalid_{field_name}", str(value))
        return default


def _fresh_id(raw: str, prefix: str, index: int, taken: set[str]) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]", "", raw or "")[:20]
    if not candidate or candidate in taken:
        candidate = f"{prefix}{index}"
        while candidate in taken:
            index += 1
            candidate = f"{prefix}{index}"
    taken.add(candidate)
    return candidate


def _unique_ids(items: list, prefix: str) -> list:
    taken: set[str] = set()
    for index, item in enumerate(items, 1):
        item.id = _fresh_id(item.id, prefix, index, taken)
    return items


def _entities(draft: PlanDraft, note) -> list[Entity]:
    entities: list[Entity] = []
    seen: set[str] = set()
    taken: set[str] = set()
    for index, raw in enumerate(draft.entities, 1):
        name = normalize_ws(raw.name)[:120]
        key = compact(name)
        if not name or not key:
            continue
        if key in seen:
            note("duplicate_entity", name)
            continue
        seen.add(key)
        aliases = [
            alias
            for alias in dict.fromkeys(normalize_ws(item)[:120] for item in raw.aliases)
            if alias and compact(alias) and compact(alias) != key
        ][:6]
        entities.append(
            Entity(
                id=_fresh_id(raw.id, "e", index, taken),
                name=name,
                type=normalize_ws(raw.type)[:80],
                aliases=aliases,
            )
        )
    return entities


def _entity_lookup(entities: list[Entity]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for entity in entities:
        lookup[entity.id.lower()] = entity.id
        for name in entity.names():
            lookup.setdefault(compact(name), entity.id)
    return lookup


def _resolve_entity_ids(raw_ids: list[str], lookup: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    for raw in raw_ids:
        entity_id = lookup.get(str(raw).strip().lower()) or lookup.get(compact(str(raw)))
        if entity_id and entity_id not in resolved:
            resolved.append(entity_id)
    return resolved


def _constraints(draft: PlanDraft, query: str, note) -> list[Constraint]:
    query_tokens = token_set(query)
    constraints: list[Constraint] = []
    taken: set[str] = set()
    for index, raw in enumerate(draft.constraints, 1):
        text = normalize_ws(raw.text)[:200]
        constraint_tokens = token_set(text)
        if not constraint_tokens:
            continue
        if len(constraint_tokens & query_tokens) / len(constraint_tokens) < 0.6:
            note("constraint_not_in_query", text)
            continue
        constraints.append(Constraint(id=_fresh_id(raw.id, "k", index, taken), text=text))
    return constraints


def _ref_lookup(constraints: list[Constraint], criteria: list[DecisionCriterion]) -> dict[str, str]:
    """What a justification or serving reference may be written as, mapped to the id it means.

    A reference means the same thing whatever its case, spacing or punctuation, and a model
    that writes a constraint's or criterion's own text instead of its id still names it.
    Ids are entered first so that an id always wins over a text that happens to read the same.
    """
    lookup = {"query": "query"}
    for item in (*constraints, *criteria):
        lookup.setdefault(compact(item.id), item.id)
    for item in (*constraints, *criteria):
        lookup.setdefault(compact(item.text), item.id)
    return lookup


def _resolve_refs(raw_refs: list[str], lookup: dict[str, str]) -> list[str]:
    return list(dict.fromkeys(lookup[key] for ref in raw_refs if (key := compact(ref)) in lookup))


def _dimensions(
    draft: PlanDraft, refs: dict[str, str], note, adopt: frozenset[str] = frozenset()
) -> tuple[list[Dimension], dict[str, str]]:
    """The proposal's dimensions that are justified, and how targets may name them.

    ``adopt`` holds the forms (id or name) of dimensions a core target names. An unjustified
    dimension among them is kept as justified by the query when ``validate_plan`` is asked to
    adopt: a core target asks what the answer depends on, so its aspect matters to the query.
    """
    dimensions: list[Dimension] = []
    remap: dict[str, str] = {}
    taken: set[str] = set()
    for index, raw in enumerate(draft.dimensions, 1):
        name = normalize_ws(raw.name)[:100]
        if not name:
            continue
        description = normalize_ws(raw.description)[:300]
        justified_by = _resolve_refs(raw.justification_refs, refs)
        if not justified_by and {compact(raw.id), compact(name)} & adopt:
            justified_by = ["query"]
            note("dimension_adopted_by_core_target", name)
        if not justified_by:
            note(
                "dimension_unjustified",
                f"{name}: justification_refs name none of {', '.join(sorted(set(refs.values())))}",
            )
            continue
        signature = token_set(f"{name} {description}")
        duplicate = next(
            (
                existing
                for existing in dimensions
                if compact(existing.name) == compact(name)
                or jaccard(token_set(f"{existing.name} {existing.description}"), signature) >= 0.7
            ),
            None,
        )
        target = duplicate.id if duplicate is not None else _fresh_id(raw.id, "m", index, taken)
        remap[raw.id or name] = target
        remap[target] = target
        # A target may name its dimension by the id in another case, or by the dimension's
        # name. Those forms never displace an id that some dimension actually carries.
        for form in (raw.id, name):
            if compact(form):
                remap.setdefault(compact(form), target)
        if duplicate is not None:
            note("duplicate_dimension", f"{name} merged into {duplicate.name}")
            continue
        dimensions.append(
            Dimension(
                id=target, name=name, description=description, justification_refs=justified_by
            )
        )
    return dimensions, remap


def _targets(
    draft: PlanDraft,
    query: str,
    dimensions: list[Dimension],
    dimension_remap: dict[str, str],
    entities: list[Entity],
    entity_lookup: dict[str, str],
    refs: dict[str, str],
    constraints: list[Constraint],
    criteria: list[DecisionCriterion],
    note,
) -> list[EvidenceTarget]:
    constraint_ids = {c.id for c in constraints}
    criterion_ids = {c.id for c in criteria}
    targets: list[EvidenceTarget] = []
    taken: set[str] = set()
    for index, raw in enumerate(draft.targets, 1):
        text = normalize_ws(raw.text)[:300]
        if len(token_set(text)) < 2:
            continue
        if _META_REFERENCE.search(text):
            note("meta_target", text)
            continue
        tier = _coerce(Tier, raw.tier, Tier.SUPPORTING, note, "tier")
        dimension_id = dimension_remap.get(raw.dimension_id) or dimension_remap.get(
            compact(raw.dimension_id)
        )
        if dimension_id is None:
            dimension_id = _infer_dimension(text, dimensions)
            if dimension_id is None:
                # Code cannot tell where the target belongs. Losing one the answer depends on
                # is worth asking the model about; a lesser one is simply dropped.
                given = raw.dimension_id or "(missing)"
                note(
                    "target_without_dimension",
                    f"dimension_id {given} is not a kept dimension: {text}",
                    tier != Tier.CORE,
                )
                continue
            note("target_dimension_inferred", text)

        subject_ids = _resolve_entity_ids(raw.subject_ids, entity_lookup)
        if len(subject_ids) < len(raw.subject_ids):
            note("target_subject_unknown", text)
        for entity in entities:
            if entity.id not in subject_ids and mentions_any(text, name_forms(entity.names())):
                subject_ids.append(entity.id)
                note("target_subjects_completed", f"{text} +{entity.name}")

        form = _coerce(TargetForm, raw.form, TargetForm.SINGLE, note, "target_form")
        if form == TargetForm.SINGLE and len(subject_ids) > 1:
            form = TargetForm.PER_SUBJECT
            note("single_target_with_several_subjects", text)
        if form != TargetForm.SINGLE and len(subject_ids) < 2:
            form = TargetForm.SINGLE
            note("target_form_needs_subjects", text)

        profile = _profile(raw, note)
        hypotheses = TargetHypotheses(
            likely_issuers=[normalize_ws(v)[:120] for v in raw.hypotheses.likely_issuers][:5],
            record_title_words=[normalize_ws(v)[:60] for v in raw.hypotheses.record_title_words][
                :8
            ],
            expected_answer=normalize_ws(raw.hypotheses.expected_answer)[:300],
        )
        dimension = next(d for d in dimensions if d.id == dimension_id)
        if _presupposes(text, hypotheses, query, entities, dimension, raw.scope_note):
            note("target_presupposes_answer", text, False)

        candidate = EvidenceTarget(
            id=_fresh_id(raw.id, "t", index, taken),
            text=text,
            dimension_id=dimension_id,
            tier=tier,
            form=form,
            subject_ids=subject_ids,
            scope_note=normalize_ws(raw.scope_note)[:200],
            profile=profile,
            hypotheses=hypotheses,
            constraint_ids=[
                ref for ref in _resolve_refs(raw.constraint_ids, refs) if ref in constraint_ids
            ],
            criterion_ids=[
                ref for ref in _resolve_refs(raw.criterion_ids, refs) if ref in criterion_ids
            ],
        )
        # A comparison of subjects needs different evidence from the same question asked of each
        # subject, however alike the wording, so the two are never merged.
        duplicate = next(
            (
                t
                for t in targets
                if jaccard(token_set(t.text), token_set(text)) >= 0.8
                and (t.form == TargetForm.JOINT) == (candidate.form == TargetForm.JOINT)
            ),
            None,
        )
        if duplicate is not None:
            if list(Tier).index(candidate.tier) < list(Tier).index(duplicate.tier):
                duplicate.tier = candidate.tier
            for field_name in ("subject_ids", "constraint_ids", "criterion_ids"):
                merged = list(
                    dict.fromkeys(
                        [*getattr(duplicate, field_name), *getattr(candidate, field_name)]
                    )
                )
                setattr(duplicate, field_name, merged)
            if duplicate.form == TargetForm.SINGLE and len(duplicate.subject_ids) > 1:
                # The same question asked of different subjects: one target, answered per subject.
                duplicate.form = TargetForm.PER_SUBJECT
            note("duplicate_target", text)
            continue
        targets.append(candidate)
    return targets


def _profile(raw: DraftTarget, note) -> EvidenceProfile:
    recency_kind = _coerce(RecencyKind, raw.recency, RecencyKind.TIMELESS, note, "recency")
    max_age: int | None = None
    if recency_kind == RecencyKind.CURRENT:
        try:
            max_age = int(float(raw.max_age_days)) if raw.max_age_days else None
        except ValueError:
            max_age = None
        if max_age is None or max_age <= 0:
            max_age = _DEFAULT_CURRENT_AGE_DAYS
        max_age = min(max_age, 10 * 365)
    authority = _coerce(
        AuthorityKind, raw.authority_kind, AuthorityKind.ISSUER, note, "authority_kind"
    )
    originator = _coerce(
        OriginatorKind, raw.originator, OriginatorKind.OTHER, note, "originator"
    )
    outcome_role = _coerce(
        OutcomeRole, raw.outcome_role, OutcomeRole.NEITHER, note, "outcome_role"
    )
    claim_scope = _coerce(ClaimScope, raw.claim_scope, ClaimScope.RECORD, note, "claim_scope")
    record_form = _coerce(RecordForm, raw.record_form, RecordForm.OTHER, note, "record_form")
    authority, claim_scope = _reconcile(authority, claim_scope, record_form, note)
    return EvidenceProfile(
        authority_kind=authority,
        claim_scope=claim_scope,
        origin_hint=_coerce(OriginHint, raw.origin_hint, OriginHint.UNKNOWN, note, "origin_hint"),
        record_form=record_form,
        originator=originator,
        recency=Recency(kind=recency_kind, max_age_days=max_age),
        error_prone=_coerce(ErrorProne, raw.error_prone, ErrorProne.NONE, note, "error_prone"),
        outcome_role=outcome_role,
    )


#: Forms that state their own content: a document of this kind settles what it says, and what
#: it says is a record, never a field's collective view.
_SELF_STATING_FORMS = frozenset(
    {
        RecordForm.DOCUMENTATION,
        RecordForm.RELEASE_NOTES,
        RecordForm.FILING,
        RecordForm.LEGAL_TEXT,
        RecordForm.ANNOUNCEMENT,
    }
)


def _reconcile(
    authority: AuthorityKind, claim_scope: ClaimScope, record_form: RecordForm, note
) -> tuple[AuthorityKind, ClaimScope]:
    """Settle a profile whose parts contradict each other.

    A planner sometimes asks for the strongest-sounding evidence rather than the evidence that
    fits: what a subject's own documentation, filing or release notes state is a record of what
    it says, and no amount of searching turns that into a consensus of a field. Left as it was,
    such a target could never be established by any source that exists.
    """
    if record_form in _SELF_STATING_FORMS and (
        authority == AuthorityKind.CONSENSUS or claim_scope == ClaimScope.GENERAL
    ):
        note(
            "profile_reconciled",
            f"{record_form.value} states its own content, so the target is a record claim",
        )
        return AuthorityKind.ISSUER, ClaimScope.RECORD
    return authority, claim_scope


def _presupposes(
    text: str,
    hypotheses: TargetHypotheses,
    query: str,
    entities: list[Entity],
    dimension: Dimension,
    scope_note: str,
) -> bool:
    """Whether a target's wording states the answer the planner expects.

    Words the target shares with the planner's expected answer, but that neither the query,
    the subjects' names, the dimension nor the scope introduced, are the answer leaking into
    the question. A search worded from such a target finds pages that agree with the guess.
    """
    expected = token_set(hypotheses.expected_answer)
    if not expected:
        return False
    known = token_set(query) | token_set(f"{dimension.name} {dimension.description} {scope_note}")
    for entity in entities:
        known |= token_set(" ".join(entity.names()))
    return len((token_set(text) & expected) - known) >= 2


def _pair_outcomes(targets: list[EvidenceTarget], note) -> None:
    """Both halves of "did it work without causing harm" are core, or neither is.

    A question of that shape is not answered by settling the good half and gesturing at the
    other: an answer that ignores the cost is not an answer. Whichever half the planner ranked
    lower is raised to match, so research pursues both and the answer waits for both.
    """
    roles = {role: [t for t in targets if t.profile.outcome_role == role] for role in OutcomeRole}
    benefits, harms = roles[OutcomeRole.BENEFIT], roles[OutcomeRole.HARM]
    if not benefits or not harms:
        return
    if not any(t.tier == Tier.CORE for t in (*benefits, *harms)):
        return
    for target in (*benefits, *harms):
        if target.tier != Tier.CORE:
            target.tier = Tier.CORE
            note("outcome_half_promoted", target.text)


def _clamp_tiers(targets: list[EvidenceTarget], depth: DepthMode, note) -> list[EvidenceTarget]:
    caps = POLICIES[depth].planning
    limit = {Tier.CORE: caps.core, Tier.SUPPORTING: caps.supporting, Tier.CONTEXT: caps.context}
    kept: list[EvidenceTarget] = []
    for tier in Tier:
        of_tier = [t for t in targets if t.tier == tier]
        # A cap may drop questions; it may not drop one half of a question that has two, which
        # would leave the answer looking settled while its cost went unasked.
        of_tier.sort(key=lambda t: t.profile.outcome_role == OutcomeRole.NEITHER)
        if len(of_tier) > limit[tier]:
            note(
                "target_budget_clamp", f"kept {limit[tier]} of {len(of_tier)} {tier.value} targets"
            )
        kept.extend(of_tier[: limit[tier]])
    return [target for target in targets if target in kept]


def _children(targets: list[EvidenceTarget], entities: list[Entity]) -> list[EvidenceTarget]:
    """One child per subject of a per-subject or joint target.

    Primary evidence is published per subject, so each side of a comparison is researched on
    its own terms, and a joint target is answered from its sides as well as from sources that
    compare them.
    """
    names = {entity.id: entity.name for entity in entities}
    children: list[EvidenceTarget] = []
    for target in targets:
        if target.form == TargetForm.SINGLE:
            continue
        for subject_id in target.subject_ids:
            children.append(
                target.model_copy(
                    deep=True,
                    update={
                        "id": f"{target.id}.{subject_id}",
                        "text": f"{target.text} (for {names.get(subject_id, subject_id)})",
                        "form": TargetForm.SINGLE,
                        "subject_ids": [subject_id],
                        "parent_id": target.id,
                    },
                )
            )
    return children


def _complexity(plan: ResearchPlan, contested_likely: bool) -> PlanComplexity:
    """Top-level targets grouped so different groups share no subject and no dimension."""
    top = plan.top_targets()
    parent = {t.id: t.id for t in top}

    def find(target_id: str) -> str:
        while parent[target_id] != target_id:
            parent[target_id] = parent[parent[target_id]]
            target_id = parent[target_id]
        return target_id

    for index, left in enumerate(top):
        for right in top[index + 1 :]:
            shares = left.dimension_id == right.dimension_id or bool(
                set(left.subject_ids) & set(right.subject_ids)
            )
            if shares:
                parent[find(right.id)] = find(left.id)
    groups: dict[str, list[str]] = {}
    for target in top:
        groups.setdefault(find(target.id), []).append(target.id)
    return PlanComplexity(
        separable_groups=list(groups.values()),
        contested_likely=contested_likely,
        core_count=sum(1 for t in top if t.tier == Tier.CORE),
    )


def _infer_dimension(text: str, dimensions: list[Dimension]) -> str | None:
    target_tokens = token_set(text)
    scored = [
        (len(target_tokens & token_set(f"{d.name} {d.description}")), d.id) for d in dimensions
    ]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


# --- expansion ---------------------------------------------------------------------------

#: Targets one expansion call may add. Expansion widens a plan; it never rewrites it.
MAX_EXPANSION_TARGETS = 3

EXPAND_INSTRUCTIONS = (
    """\
Research has answered what the plan asked, and what it read raised something the plan did not.

The INPUT gives the query, the targets researched so far with what the evidence established,
and the dimensions of the plan. Propose further research targets, using the same fields as a
plan's targets, only where the research itself showed one is needed: a prerequisite the
evidence names and leaves unanswered, a competing explanation the sources raise, or the cause
of a disagreement between them.

Propose nothing when the answer is complete as it stands: an empty list is the usual answer,
and adding a target that merely restates one already answered is wrong. Never propose a
target whose wording states the answer you expect. Use existing dimension ids.
Output: {"targets": [...]}
"""
    + _PROFILE_FIELDS
)


def may_expand(plan: ResearchPlan, policy, calls_used: int, groups=()) -> bool:
    """Whether this mode may ask for more targets now.

    Standard expands only when the research met something it could not settle; Deep may
    reflect on any saturated plan. Both are bounded by the policy's call allowance.
    """
    if calls_used >= policy.expansion_calls:
        return False
    if not policy.expansion_requires_cause:
        return True
    return bool(plan.complexity.contested_likely or list(groups))


def expand(
    plan: ResearchPlan,
    summary: list[dict],
    depth: DepthMode,
    *,
    llm_factory: LLMFactory,
    budget: LLMBudget,
    trace: ResearchTrace,
) -> list[EvidenceTarget]:
    """Targets the evidence itself showed were missing, validated as a plan's targets are.

    Returns only what survives validation, already appended to ``plan`` with its children.
    """
    payload = {
        "query": plan.query,
        "task_type": plan.understanding.task_type.value,
        "entities": [
            {"id": e.id, "name": e.name, "type": e.type, "aliases": e.aliases}
            for e in plan.understanding.entities
        ],
        "dimensions": [
            {"id": d.id, "name": d.name, "description": d.description}
            for d in plan.decomposition.dimensions
        ],
        "targets_so_far": summary,
        "limits": {"max_new_targets": MAX_EXPANSION_TARGETS},
    }
    try:
        draft = call_json(
            llm_factory,
            stage="research_expansion",
            instructions=EXPAND_INSTRUCTIONS,
            payload=payload,
            schema=TargetsDraft,
            budget=budget,
            num_predict=1200,
        )
    except (ModelOutputInvalid, LLMBudgetExhausted) as exc:
        trace.errors.append(f"expansion skipped: {exc}")
        return []

    issues: list[PlanIssue] = []

    def note(code: str, detail: str = "", repaired: bool = True) -> None:
        issues.append(PlanIssue(code=code, detail=normalize_ws(detail)[:300], repaired=repaired))

    existing = plan.targets
    taken = {target.id for target in existing}
    dimension_ids = {d.id: d for d in plan.decomposition.dimensions}
    entity_lookup = _entity_lookup(plan.understanding.entities)
    added: list[EvidenceTarget] = []
    for index, raw in enumerate(draft.targets[: MAX_EXPANSION_TARGETS * 2], 1):
        if len(added) >= MAX_EXPANSION_TARGETS:
            break
        text = normalize_ws(raw.text)[:300]
        if len(token_set(text)) < 2 or _META_REFERENCE.search(text):
            note("meta_target", text)
            continue
        dimension = dimension_ids.get(raw.dimension_id) or dimension_ids.get(
            _infer_dimension(text, plan.decomposition.dimensions) or ""
        )
        if dimension is None:
            note("target_without_dimension", text)
            continue
        if any(jaccard(token_set(t.text), token_set(text)) >= 0.7 for t in existing):
            note("duplicate_target", text)
            continue
        profile = _profile(raw, note)
        hypotheses = TargetHypotheses(
            likely_issuers=[normalize_ws(v)[:120] for v in raw.hypotheses.likely_issuers][:3],
            record_title_words=[normalize_ws(v)[:60] for v in raw.hypotheses.record_title_words][
                :6
            ],
            expected_answer=normalize_ws(raw.hypotheses.expected_answer)[:200],
        )
        scope_note = normalize_ws(raw.scope_note)[:200]
        if _presupposes(
            text,
            hypotheses,
            plan.query,
            plan.understanding.entities,
            dimension,
            scope_note,
        ):
            note("target_presupposes_answer", text, repaired=False)
            continue
        subject_ids = _resolve_entity_ids(raw.subject_ids, entity_lookup)
        form = _coerce(TargetForm, raw.form, TargetForm.SINGLE, note, "form")
        if form != TargetForm.JOINT and len(subject_ids) > 1:
            form = TargetForm.PER_SUBJECT
        # Expansion widens the edges of a plan: what the core answer needs was planned already.
        proposed = _coerce(Tier, raw.tier, Tier.CONTEXT, note, "tier")
        tier = Tier.SUPPORTING if proposed == Tier.CORE else proposed
        target = EvidenceTarget(
            id=_fresh_id(raw.id, "x", index, taken),
            text=text,
            dimension_id=dimension.id,
            tier=tier,
            form=form,
            subject_ids=subject_ids,
            scope_note=scope_note,
            profile=profile,
            hypotheses=hypotheses,
        )
        taken.add(target.id)
        added.append(target)
    if not added:
        plan.issues.extend(issues)
        return []
    children = _children(added, plan.understanding.entities)
    plan.targets = [*plan.targets, *added, *children]
    plan.complexity = _complexity(plan, plan.complexity.contested_likely)
    plan.issues.extend(
        [*issues, PlanIssue(code="targets_added_by_expansion", detail=str(len(added)))]
    )
    return [*added, *children]
