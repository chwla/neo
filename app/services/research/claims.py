"""Claims: what the report may say, synthesized only from accepted evidence ids."""

from __future__ import annotations

import re
from collections import defaultdict

from pydantic import BaseModel, ConfigDict, Field

from app.services.research.capability import items_per_call
from app.services.research.llm_json import (
    LLMBudget,
    LLMBudgetExhausted,
    LLMFactory,
    ModelOutputInvalid,
    call_json,
)
from app.services.research.textutil import compact, jaccard, normalize_ws, token_set
from app.services.research.types import (
    Basis,
    Claim,
    ClaimFit,
    ClaimType,
    ComparisonDimension,
    EvidenceItem,
    Importance,
    Polarity,
    ResearchPlan,
    ResearchTrace,
    Strength,
    Sufficiency,
    Support,
    TargetState,
    Tier,
)

#: Evidence per synthesis call on a capable model; a smaller model gets fewer.
MAX_EVIDENCE_PER_CALL = 40
#: Output one claim costs: a sentence, its ids and its scope.
CLAIM_OUTPUT_TOKENS = 120
MAX_EVIDENCE_PER_CELL = 6

CLAIM_INSTRUCTIONS = """\
Build the claims a research report may make, using only the accepted evidence in the INPUT.

- Every claim cites evidence_ids from the INPUT that together state it. Use no outside knowledge.
- claim_type: fact, comparison (how two or more entities differ or are alike on the same
  aspect), tradeoff (a gain on one aspect comes with a cost on another), mechanism (how
  something works), limitation, negative (something is not the case), recommendation, or
  uncertainty. A statement about two or more entities on the same aspect is a comparison.
- A comparison needs evidence about every compared entity on the same aspect, or evidence
  that compares them directly. When evidence covers only some entities, state facts about
  those entities instead.
- For a comparative question where evidence covers every compared entity on the same
  aspect, also write the comparison or tradeoff that the evidence supports.
- A negative claim needs evidence that explicitly says so. Never turn missing evidence into
  a negative.
- When evidence items disagree, do not pick a side: write an uncertainty claim that
  describes the disagreement, cite both sides, and list them under conflicts.
- Recommendations only when the task asks for a decision. Make them conditional on a user
  priority or constraint (put it in condition), list supporting_claim_ids, and never name an
  overall winner when evidence about the options is incomplete or one-sided.
- Keep each claim within the strength of its evidence; partial support means a hedged claim.
- Say only what the cited excerpts themselves say. Never carry a subject, treatment, product,
  population, condition or qualifier over from the question into a claim when the excerpts
  do not state it: evidence that does not say what it is about cannot answer the question.
- State each fact once: when several evidence items say the same thing, write one claim
  citing all of them.
- Evidence limited to different scopes (a place, population, version or condition) does
  not merge into one general claim: write separate claims and name each stated scope.
- Cover every question that has evidence: write at least one claim for each question whose
  evidence_ids support one, and as many distinct claims as the evidence genuinely
  supports. A simple question may need one; a rich one may need several. Merge evidence
  that says the same thing into a single claim rather than restating it.
- question_id is the question the claim answers (null only for recommendations drawing on
  several questions). entity_ids are the entities the claim is about.
- Write plain sentences without citation markers.

Output: {"claims": [{"id", "text", "claim_type", "polarity", "question_id", "entity_ids",
"evidence_ids", "supporting_claim_ids", "condition"}], "conflicts": [{"evidence_ids": [...],
"detail": "..."}]}
"""


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class _DraftClaim(_Lenient):
    id: str = ""
    text: str = ""
    claim_type: str = "fact"
    polarity: str = "affirms"
    question_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] = Field(default_factory=list)
    condition: str | None = None


class _DraftConflict(_Lenient):
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class _ClaimOutput(_Lenient):
    claims: list[_DraftClaim] = Field(default_factory=list)
    conflicts: list[_DraftConflict] = Field(default_factory=list)


_FIT_RANK = {ClaimFit.ESTABLISHES: 0, ClaimFit.SUPPORTS: 1, ClaimFit.CONTEXT: 2, None: 3}
_SUPPORT_RANK = {Support.DIRECT: 0, Support.PARTIAL: 1, Support.NONE: 2}


def build_claims(
    plan: ResearchPlan,
    evidence: list[EvidenceItem],
    states: dict[str, TargetState] | None,
    *,
    llm_factory: LLMFactory,
    llm_budget: LLMBudget,
    trace: ResearchTrace,
) -> tuple[list[Claim], list[tuple[str, str]]]:
    accepted = [
        item
        for item in evidence
        if item.status == "accepted"
        and item.duplicate_of is None
        and item.claim_fit != ClaimFit.INSUFFICIENT
    ]
    if not accepted:
        return [], []
    selected = _select_evidence(plan, accepted)
    batches = _batches(plan, selected, llm_budget)
    drafts: list[_DraftClaim] = []
    conflict_links: list[tuple[str, str]] = []
    for batch in batches:
        payload = {
            "query": plan.query,
            "task_type": plan.understanding.task_type.value,
            "answer_shape": plan.understanding.answer_shape,
            "entities": [
                {"id": e.id, "name": e.name, "type": e.type} for e in plan.understanding.entities
            ],
            "constraints": [c.model_dump() for c in plan.understanding.constraints],
            "decision_criteria": [c.model_dump() for c in plan.understanding.decision_criteria],
            "dimensions": [{"id": d.id, "name": d.name} for d in plan.decomposition.dimensions],
            "questions": [
                {
                    "id": t.id,
                    "text": t.text,
                    "dimension_id": t.dimension_id,
                    "form": t.form.value,
                    "importance": t.tier.value,
                    "entity_ids": t.subject_ids,
                    "scope": t.scope_note,
                    "evidence_ids": [item.id for item in batch if item.question_id == t.id],
                }
                for t in plan.targets
            ],
            "coverage": [
                {
                    "question_id": state.target_id,
                    "sufficiency": state.sufficiency.value,
                    "basis": state.basis.value,
                }
                for state in (states or {}).values()
            ],
            "evidence": [_evidence_payload(item) for item in batch],
        }
        try:
            output = call_json(
                llm_factory,
                stage="claim_synthesis",
                instructions=CLAIM_INSTRUCTIONS,
                payload=payload,
                schema=_ClaimOutput,
                budget=llm_budget,
                num_predict=6000,
                notes=trace.errors,
                reserved=True,
            )
        except (ModelOutputInvalid, LLMBudgetExhausted) as exc:
            trace.errors.append(f"claim synthesis batch skipped: {exc}")
            continue
        drafts.extend(output.claims)
        for conflict in output.conflicts:
            ids = [item for item in conflict.evidence_ids if isinstance(item, str)]
            conflict_links.extend(zip(ids, ids[1:], strict=False))
    return _normalize(plan, drafts), conflict_links


def build_comparison(
    plan: ResearchPlan, claims: list[Claim], states: dict[str, TargetState] | None
) -> list[ComparisonDimension]:
    """Deterministic comparison frame: one row per dimension, one column per entity."""
    entities = [e.id for e in plan.understanding.entities]
    if len(entities) < 2:
        return []
    states = states or {}
    rows: list[ComparisonDimension] = []
    for dimension in plan.decomposition.dimensions:
        targets = [t for t in plan.targets if t.dimension_id == dimension.id]
        question_ids = {t.id for t in targets}
        involved = [e for e in entities if any(e in t.subject_ids for t in targets)]
        if len(involved) < 2:
            continue
        row = ComparisonDimension(
            dimension_id=dimension.id,
            name=dimension.name,
            importance=Importance.CORE
            if any(t.tier == Tier.CORE for t in targets)
            else Importance.SUPPORTING,
        )
        for entity_id in involved:
            row.entity_claim_ids[entity_id] = [
                claim.id
                for claim in claims
                if claim.question_id in question_ids
                and claim.entity_ids == [entity_id]
                and claim.claim_type
                in (ClaimType.FACT, ClaimType.NEGATIVE, ClaimType.LIMITATION, ClaimType.MECHANISM)
            ]
            cell_strengths = [
                strength_of(states[t.id])
                for t in targets
                if t.subject_ids == [entity_id] and t.id in states
            ] or [strength_of(states[t.id]) for t in targets if t.id in states]
            if cell_strengths:
                row.strength_by_entity[entity_id] = max(
                    cell_strengths,
                    key=lambda s: ["missing", "weak", "medium", "strong"].index(s.value),
                )
        row.comparison_claim_ids = [
            claim.id
            for claim in claims
            if claim.question_id in question_ids
            and claim.claim_type in (ClaimType.COMPARISON, ClaimType.TRADEOFF)
        ]
        rows.append(row)
    return rows


def strength_of(state: TargetState) -> Strength:
    """How firmly a target is established, on the scale the comparison frame shows."""
    if state.sufficiency == Sufficiency.INSUFFICIENT:
        return Strength.WEAK if state.evidence_ids else Strength.MISSING
    if state.sufficiency == Sufficiency.HEDGED:
        return Strength.WEAK
    return (
        Strength.STRONG
        if state.basis == Basis.DECISIVE or state.has_provenance_a
        else Strength.MEDIUM
    )


def _select_evidence(plan: ResearchPlan, accepted: list[EvidenceItem]) -> list[EvidenceItem]:
    by_cell: dict[tuple[str, tuple[str, ...]], list[EvidenceItem]] = defaultdict(list)
    for item in accepted:
        by_cell[(item.question_id, tuple(sorted(item.entity_ids)))].append(item)
    chosen: list[EvidenceItem] = []
    for items in by_cell.values():
        items.sort(
            key=lambda item: (
                item.contradiction_group is None,
                _SUPPORT_RANK[item.support],
                _FIT_RANK.get(item.claim_fit, 3),
                item.id,
            )
        )
        # Conflicting evidence first so disagreements are never trimmed away.
        chosen.extend(items[:MAX_EVIDENCE_PER_CELL])
    return chosen


def _batches(
    plan: ResearchPlan, items: list[EvidenceItem], budget: LLMBudget | None = None
) -> list[list[EvidenceItem]]:
    """Split evidence into calls whose answers the routed model can finish."""
    per_call = MAX_EVIDENCE_PER_CALL
    if budget is not None:
        per_call = min(
            per_call,
            items_per_call(
                budget.capability,
                output_tokens_per_item=CLAIM_OUTPUT_TOKENS,
                overhead_tokens=200,
                ceiling=MAX_EVIDENCE_PER_CALL,
            ),
        )
    if len(items) <= per_call:
        return [items]
    by_dimension: dict[str, list[EvidenceItem]] = defaultdict(list)
    for item in items:
        question = plan.target(item.question_id)
        by_dimension[question.dimension_id if question else ""].append(item)
    batches: list[list[EvidenceItem]] = [[]]
    for group in by_dimension.values():
        if batches[-1] and len(batches[-1]) + len(group) > per_call:
            batches.append([])
        batches[-1].extend(group[:per_call])
    return batches


def _normalize(plan: ResearchPlan, drafts: list[_DraftClaim]) -> list[Claim]:
    lookup = {e.id: e.id for e in plan.understanding.entities}
    for entity in plan.understanding.entities:
        for name in entity.names():
            lookup.setdefault(compact(name), entity.id)
    id_map: dict[str, str] = {}
    claims: list[Claim] = []
    by_text: dict[tuple[str, ClaimType], Claim] = {}
    for draft in drafts:
        text = normalize_ws(draft.text)[:500]
        if not text:
            continue
        try:
            claim_type = ClaimType(draft.claim_type.strip().lower())
        except ValueError:
            claim_type = ClaimType.FACT
        entity_ids = list(
            dict.fromkeys(
                entity_id
                for raw in draft.entity_ids
                if (entity_id := lookup.get(raw) or lookup.get(compact(raw)))
            )
        )
        question_id = draft.question_id if plan.target(draft.question_id) else None
        existing = by_text.get((text.lower(), claim_type)) or _restatement_of(
            claims, text, claim_type, question_id, entity_ids
        )
        if existing is not None:
            # The same statement drawn from several questions is one claim with more support.
            existing.evidence_ids = list(
                dict.fromkeys([*existing.evidence_ids, *draft.evidence_ids])
            )
            existing.entity_ids = list(dict.fromkeys([*existing.entity_ids, *entity_ids]))
            if draft.id:
                id_map[draft.id] = existing.id
            continue
        claim_id = f"c{len(claims) + 1}"
        if draft.id:
            id_map[draft.id] = claim_id
        polarity = (
            Polarity.NEGATES if draft.polarity.strip().lower() == "negates" else Polarity.AFFIRMS
        )
        claim = Claim(
            id=claim_id,
            text=text,
            claim_type=claim_type,
            polarity=polarity,
            question_id=draft.question_id if plan.target(draft.question_id) else None,
            entity_ids=entity_ids,
            evidence_ids=list(dict.fromkeys(draft.evidence_ids)),
            supporting_claim_ids=list(draft.supporting_claim_ids),
            condition=normalize_ws(draft.condition or "")[:200] or None,
        )
        claims.append(claim)
        by_text[(text.lower(), claim_type)] = claim
    for claim in claims:
        claim.supporting_claim_ids = list(
            dict.fromkeys(
                id_map.get(raw, raw)
                for raw in claim.supporting_claim_ids
                if id_map.get(raw, raw) != claim.id
            )
        )
    return claims


def _restatement_of(
    claims: list[Claim],
    text: str,
    claim_type: ClaimType,
    question_id: str | None,
    entity_ids: list[str],
) -> Claim | None:
    """An existing claim that says the same thing: same question, entities, type and numbers."""
    if claim_type == ClaimType.RECOMMENDATION:
        return None
    words = token_set(text)
    numbers = sorted(re.findall(r"\d+(?:[.,]\d+)?", text))
    for claim in claims:
        if (
            claim.claim_type == claim_type
            and claim.question_id == question_id
            and set(claim.entity_ids) == set(entity_ids)
            and sorted(re.findall(r"\d+(?:[.,]\d+)?", claim.text)) == numbers
            and jaccard(token_set(claim.text), words) >= 0.75
        ):
            return claim
    return None


def _evidence_payload(item: EvidenceItem) -> dict:
    return {
        "id": item.id,
        "question_id": item.question_id,
        "entity_ids": item.entity_ids,
        "claim": item.claim,
        "polarity": item.polarity.value,
        "attribute": item.attribute,
        "value": item.value,
        "unit": item.unit,
        "scope": item.scope,
        "qualifiers": item.qualifiers,
        "as_of": item.as_of,
        "support": item.support.value,
        "fit": item.claim_fit.value if item.claim_fit else "unassessed",
        "freshness": item.freshness_fit.value,
        # The items this one actually disagrees with; a group's other members may not.
        **(
            {"disagrees_with": item.contradicted_by}
            if item.contradicted_by is not None
            else {"contradiction_group": item.contradiction_group}
        ),
    }
