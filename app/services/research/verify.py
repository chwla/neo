"""Mandatory verification: nothing reaches the report without passing through ``audit``.

Deterministic rules check what code can prove: citations exist and are accepted evidence
for the same question, the evidence is about the entities the claim names, comparisons
have evidence for every side, negatives rest on negating evidence, recommendations rest on
supported reasons, conflicts and stale or partial evidence cap support. A model entailment
audit then checks that the cited excerpts actually say what the claim says.

Entailment is required of every claim, model-written or extractive. Where the model audit
cannot run -- out of time, out of calls, the model failing -- a strict check of the wording
stands in: every word, figure and judgement the claim states must be in its excerpts. Code
also refuses, whatever an auditor says, a judgement ("better", "best") its excerpts never
make. A claim that cannot be shown to be entailed is not deleted silently: its excerpts are
reported as unverified evidence. The renderer accepts only a :class:`VerifiedClaimSet`, which
only this module can construct.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from app.services.research.capability import items_per_call
from app.services.research.claims import unsupported_specifics
from app.services.research.llm_json import (
    STAGE_FAILURES,
    LLMBudget,
    LLMFactory,
    call_json,
)
from app.services.research.policy import ResearchPolicy
from app.services.research.textutil import (
    compact,
    jaccard,
    mentions_any,
    name_forms,
    names_as_words,
    names_entity,
    normalize_ws,
    scope_conflict,
    scope_tokens,
    stem,
    stem_set,
    token_set,
)
from app.services.research.types import (
    Claim,
    ClaimFit,
    ClaimScope,
    ClaimType,
    Entity,
    EvidenceItem,
    FreshnessFit,
    Polarity,
    Provenance,
    RecencyKind,
    ResearchPlan,
    ResearchSource,
    ResearchTrace,
    SourceRole,
    Support,
    SupportStatus,
    Tier,
    TimeSensitivity,
    UnverifiedEvidence,
    VerificationDecision,
)

_FACTORY_TOKEN = object()
_INFERRED_NOTE = "cited evidence does not name the entity; attribution was inferred"
#: Marks a citation set aside because its scope describes a different subject. The
#: report reads this to tell the reader that such evidence was not combined.
SCOPE_NOTE = "stated for a different scope than"
#: Marks a claim narrowed to what its evidence states, after the audit found it said more.
NARROWED_NOTE = "narrowed to what the cited evidence states"
CONTEXT_ONLY_NOTE = "cited evidence can only give context for this question"
#: Marks a claim stated of a population that only a few cases were seen of.
GENERALISED_NOTE = "stated in general from too few cases; it holds for the cases cited"
#: Marks a claim giving a figure its evidence does not, which is the part a reader repeats.
FIGURE_NOTE = "states a figure the cited evidence does not give"
CONFLICT_NOTE = "cites evidence that other sources contradict"
#: Marks a conflict the audit found to concern something other than what the claim states.
LIFTED_CONFLICT_NOTE = (
    "other sources disagree about a related point, not about what this claim states"
)
#: Marks a claim that holds only by adding several sources together, which none of them states.
UNION_NOTE = "no single cited source states this; it holds only by combining them"
_AUDIT_BATCH = 8
#: How much of a claim's wording its excerpts must contain to pass the wording check that
#: stands in for an audit the model could not run.
WORDING_ENTAILMENT = 0.75
#: Words that judge or rank rather than state. A claim may use one only where its excerpts do.
_JUDGEMENT = re.compile(
    r"\b(?:better|best|worse|worst|superior|inferior|greatest|ideal|recommended|preferable|"
    r"outperforms?|beats?|wins?|winner|leading|top|overall|everyone|always|never|"
    r"more \w+ than|less \w+ than|faster|slower|cheaper|easier|harder|stronger|weaker)\b",
    re.IGNORECASE,
)
#: Unverified excerpts shown per question, and in all.
_UNVERIFIED_PER_QUESTION, _UNVERIFIED_MAX = 3, 12
#: Output one verdict costs: a claim id and a single word.
_VERDICT_OUTPUT_TOKENS = 30
#: Claims that state a relation between sources' facts rather than repeating one of them.
_DERIVED_TYPES = frozenset({ClaimType.COMPARISON, ClaimType.TRADEOFF})
_DECISIVE_TYPES = frozenset(
    {ClaimType.COMPARISON, ClaimType.TRADEOFF, ClaimType.NEGATIVE, ClaimType.RECOMMENDATION}
)
_SUPPORT_ORDER = [
    SupportStatus.SUPPORTED,
    SupportStatus.PARTIALLY_SUPPORTED,
    SupportStatus.INHERENTLY_UNCERTAIN,
    SupportStatus.CONFLICTING_EVIDENCE,
]

ENTAILMENT_INSTRUCTIONS = """\
Check whether the cited evidence entails each claim.

Each claim comes with the verbatim excerpts it cites, each labelled with the entities it is
about, and for recommendations the claims offered as reasons. entailment is:
- full: the excerpts state everything the claim says, at the claim's strength and scope.
- partial: they support part of it, or only a weaker or narrower version.
- none: they do not support it.
A claim comparing entities is fully entailed only when, for every entity involved, an
excerpt about that entity (or one comparing them directly) states that entity's side of the
comparison. Use no outside knowledge and give no credit for inferences the excerpts do not
state.
overstated is true when the claim is stronger, broader or more certain than the excerpts.
Some claims also list opposing excerpts, from sources the claim's evidence disagrees with.
For those, contradicted is true when an opposing excerpt states the opposite of the claim, or
a materially different result, for the same subject, outcome, population and condition; it
is false when they concern a different outcome, population, condition or proposition, so
both can hold. Omit contradicted for claims with no opposing excerpts.
Output: {"results": [{"claim_id": "...", "entailment": "full|partial|none", "overstated": false,
"contradicted": false}]}
"""

REWRITE_INSTRUCTIONS = """\
Restate each claim as exactly what its cited excerpts state, and nothing more.

Keep the subject and the kind of statement. Drop or narrow every part the excerpts do not
state: a comparison they do not make, a stronger or more certain word, a wider scope, a figure
they do not give. Attribute a claim that rests on individual reports to those reports. Use at
most 25 words, add nothing the excerpts do not state, and return an empty text for a claim
whose excerpts leave nothing worth saying.
Output: {"claims": [{"claim_id": "...", "text": "..."}]}
"""


class VerifiedClaimSet:
    """Claims that survived verification. Construct only through :func:`audit`."""

    __slots__ = ("claims", "removed", "decisions", "unverified")

    def __init__(
        self,
        claims: list[Claim],
        removed: list[Claim],
        decisions: list[VerificationDecision],
        unverified: list[UnverifiedEvidence] | None = None,
        *,
        _token: object = None,
    ) -> None:
        if _token is not _FACTORY_TOKEN:
            raise TypeError("VerifiedClaimSet can only be produced by verify.audit")
        self.claims: tuple[Claim, ...] = tuple(claims)
        self.removed: tuple[Claim, ...] = tuple(removed)
        self.decisions: tuple[VerificationDecision, ...] = tuple(decisions)
        #: Accepted excerpts that no surviving claim cites, quoted rather than stated.
        self.unverified: tuple[UnverifiedEvidence, ...] = tuple(unverified or ())

    def by_id(self) -> dict[str, Claim]:
        return {claim.id: claim for claim in self.claims}


def merge_verified(first: VerifiedClaimSet, second: VerifiedClaimSet) -> VerifiedClaimSet:
    """One verified set from two audits of the same run, keeping every claim audited.

    Used when research went back for a question verification left unanswered: the later audit's
    claims join the earlier ones, and nothing that failed either audit is kept.
    """
    kept: dict[str, Claim] = {claim.id: claim for claim in (*first.claims, *second.claims)}
    removed = [claim for claim in (*first.removed, *second.removed) if claim.id not in kept]
    cited = {evidence_id for claim in kept.values() for evidence_id in claim.evidence_ids}
    unverified: dict[str, UnverifiedEvidence] = {}
    for item in (*first.unverified, *second.unverified):
        if item.evidence_id not in cited:
            unverified.setdefault(item.evidence_id, item)
    return VerifiedClaimSet(
        list(kept.values()),
        removed,
        [*first.decisions, *second.decisions],
        list(unverified.values()),
        _token=_FACTORY_TOKEN,
    )


class _Verdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim_id: str = ""
    entailment: str = "none"
    #: The claim says more than the excerpts do, even where they support its subject.
    overstated: bool = False
    #: Whether an opposing excerpt disagrees with this very claim. None when not judged.
    contradicted: bool | None = None


class _VerdictOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: list[_Verdict] = Field(default_factory=list)


class _Rewritten(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claim_id: str = ""
    text: str = ""


class _RewriteOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    claims: list[_Rewritten] = Field(default_factory=list)


def audit(
    plan: ResearchPlan,
    claims: list[Claim],
    evidence: list[EvidenceItem],
    *,
    policy: ResearchPolicy,
    llm_factory: LLMFactory,
    llm_budget: LLMBudget,
    trace: ResearchTrace,
) -> VerifiedClaimSet:
    evidence_by_id = {item.id: item for item in evidence}
    kept: dict[str, Claim] = {}
    removed: list[Claim] = []
    decisions: dict[str, VerificationDecision] = {}

    def remove(claim: Claim, reason: str) -> None:
        claim.verification_notes.append(reason)
        claim.support = SupportStatus.INSUFFICIENT_RESEARCH
        kept.pop(claim.id, None)
        removed.append(claim)
        decisions[claim.id] = VerificationDecision(
            claim_id=claim.id,
            outcome="removed",
            support=claim.support,
            reasons=list(claim.verification_notes),
        )

    def settle(claim: Claim, why: str) -> None:
        """No model audit for this claim: the wording check decides, never silence."""
        share = wording_entailment(claim, _cited(claim, evidence_by_id))
        if share < WORDING_ENTAILMENT:
            remove(claim, f"{why}, and its wording is not stated by the cited excerpts")
            return
        claim.verification_notes.append(f"{why}; checked against the excerpts' wording")
        if share < 0.95:
            _cap(claim, SupportStatus.PARTIALLY_SUPPORTED, "entailed by wording, not audited")

    ordinary = [c.model_copy(deep=True) for c in claims if c.claim_type != ClaimType.RECOMMENDATION]
    recommendations = [
        c.model_copy(deep=True) for c in claims if c.claim_type == ClaimType.RECOMMENDATION
    ]

    for claim in ordinary:
        reason = _deterministic_checks(plan, claim, evidence_by_id)
        if reason:
            remove(claim, reason)
        else:
            kept[claim.id] = claim

    # A claim in its excerpts' own words is entailed, and code can prove it: asking a small
    # model whether a sentence states itself only invites a wrong "no". Those claims skip the
    # model audit; what they say is still capped to what their evidence can carry.
    verbatim = {
        c.id
        for c in kept.values()
        if wording_entailment(c, _cited(c, evidence_by_id)) >= VERBATIM
    }
    for claim in list(kept.values()):
        if claim.id in verbatim:
            claim.verification_notes.append("states its excerpts' own words; entailed")
            if claim.origin == "extractive" and not _answers_question(plan, claim):
                remove(claim, "its excerpt may not answer the question it was found for")
                verbatim.discard(claim.id)
    audited = [
        c for c in kept.values() if _in_scope(plan, c, policy) and c.id not in verbatim
    ]
    weakened = _entailment(
        plan,
        audited,
        evidence_by_id,
        {},
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=settle,
    )
    # A judgement the excerpts never make is refused whatever the audit said: an auditor that
    # approves everything must not be able to pass "X is better than Y" off feature facts.
    judged = [
        c
        for c in kept.values()
        if c not in weakened and unsupported_judgement(c.text, _cited(c, evidence_by_id))
    ]
    for claim in judged:
        claim.verification_notes.append("judges or ranks where the cited excerpts do not")
    # Likewise a claim whose own words appear in none of its excerpts ("differ in their gameplay
    # style" over six excerpts about settings, movement and studios), whatever the audit said.
    unstated = [
        c
        for c in kept.values()
        if c not in weakened
        and c not in judged
        and c.id not in verbatim
        and unsupported_specifics(plan, c.text, _cited(c, evidence_by_id))
    ]
    for claim in unstated:
        claim.verification_notes.append("says what none of its cited excerpts say")
    judged = [*judged, *unstated]
    overstated = {CONTEXT_ONLY_NOTE, GENERALISED_NOTE, FIGURE_NOTE}
    for claim in kept.values():
        if claim.id in verbatim and overstated & set(claim.verification_notes):
            # Its own excerpt's words: nothing to narrow, only a limit to state.
            _cap(claim, SupportStatus.PARTIALLY_SUPPORTED, "holds for what its source states")
    context_only = [
        c
        for c in kept.values()
        if overstated & set(c.verification_notes)
        and c not in weakened
        and c not in judged
        and c.id not in verbatim
    ]
    _rewrite(
        plan,
        [c for c in [*weakened, *judged, *context_only] if c.id in kept],
        evidence_by_id,
        {},
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=settle,
    )
    union_only = _union_only(
        plan,
        [c for c in kept.values() if _in_scope(plan, c, policy)],
        evidence_by_id,
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
    )
    for claim in union_only:
        claim.verification_notes.append(UNION_NOTE)
    _rewrite(
        plan,
        [c for c in union_only if c.id in kept],
        evidence_by_id,
        {},
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=settle,
    )
    for claim in list(kept.values()):
        if claim.id in kept and claim.id not in verbatim and not _in_scope(plan, claim, policy):
            # This mode does not send the claim to the model audit; entailment is still
            # required of it, so the wording check decides.
            settle(claim, "not sent to the model audit at this depth")

    for claim in recommendations:
        reason = _recommendation_checks(plan, claim, kept)
        if reason:
            remove(claim, reason)
            continue
        kept[claim.id] = claim
    weakened = _entailment(
        plan,
        [c for c in recommendations if c.id in kept],
        evidence_by_id,
        kept,
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=lambda claim, why: _cap(claim, SupportStatus.PARTIALLY_SUPPORTED, why),
    )
    _rewrite(
        plan,
        [c for c in weakened if c.id in kept],
        evidence_by_id,
        kept,
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=lambda claim, why: remove(claim, why),
    )

    ordered = [kept[c.id] for c in claims if c.id in kept]
    for claim in ordered:
        outcome = "kept" if claim.support == SupportStatus.SUPPORTED else "downgraded"
        decisions.setdefault(
            claim.id,
            VerificationDecision(
                claim_id=claim.id,
                outcome=outcome,
                support=claim.support,
                reasons=list(claim.verification_notes),
            ),
        )
    unverified = _unverified(ordered, removed, evidence_by_id)
    trace.verification.extend(decisions.values())
    trace.removed_claims.extend(removed)
    return VerifiedClaimSet(
        ordered, removed, list(decisions.values()), unverified, _token=_FACTORY_TOKEN
    )


#: Wording share at which a claim is its excerpts' own words.
VERBATIM = 0.95
#: Wording share one excerpt must carry for its source to count as corroborating a claim.
SUPPORTS_ALONE = 0.5
#: Words of a question that say nothing about what is asked.
_QUESTION_FILLER = frozenset(
    {"what", "which", "who", "when", "where", "why", "how", "do", "does", "did", "is", "are",
     "was", "were", "for", "the", "and", "compare", "compared", "differ", "difference",
     "between", "both", "have", "has", "common", "about", "known"}
)


def _answers_question(plan: ResearchPlan, claim: Claim) -> bool:
    """Whether a code-written finding shares anything with what its question asks.

    A judge proposed the evidence; code wrote the finding from it. Before code states it as an
    answer, the excerpt must at least speak to the question's own terms (beyond the subjects'
    names). A question that names nothing but its subjects is answered by anything about them.
    """
    target = plan.target(claim.question_id) if claim.question_id else None
    if target is None:
        return True
    names: set[str] = set()
    for entity in plan.understanding.entities:
        names |= stem_set(" ".join(entity.names()))
    dimension = plan.dimension(target.dimension_id)
    asked = stem_set(f"{target.text} {target.scope_note}") - names - {
        stem(word) for word in _QUESTION_FILLER
    }
    if dimension is not None and dimension.name != "The question as asked":
        asked |= stem_set(dimension.name) - names
    if not asked:
        return True
    return bool(asked & stem_set(claim.text))


def _about(plan: ResearchPlan, item: EvidenceItem) -> set[str]:
    """The entities an item is about: its judge's tags, and those its excerpt names itself."""
    found = set(item.entity_ids)
    for entity in plan.understanding.entities:
        if entity.id not in found and names_as_words(item.excerpt, entity.names()):
            found.add(entity.id)
    return found


def _cited(claim: Claim, evidence_by_id: dict[str, EvidenceItem]) -> list[EvidenceItem]:
    return [evidence_by_id[e] for e in claim.evidence_ids if e in evidence_by_id]


def unsupported_judgement(text: str, cited: list[EvidenceItem]) -> bool:
    """Whether a claim judges, ranks or generalises in words none of its excerpts use."""
    claimed = {normalize_ws(m.group(0)).lower() for m in _JUDGEMENT.finditer(text or "")}
    if not claimed:
        return False
    excerpts = " ".join(item.excerpt for item in cited)
    stated = {normalize_ws(m.group(0)).lower() for m in _JUDGEMENT.finditer(excerpts)}
    return bool(claimed - stated)


def wording_entailment(claim: Claim, cited: list[EvidenceItem]) -> float:
    """How much of a claim its excerpts state word for word: 0 when it gives a figure, or a
    judgement, they do not. The check that stands in for an audit the model cannot run."""
    if not cited:
        return 0.0
    excerpts = " ".join(item.excerpt for item in cited)
    values = " ".join(item.value or "" for item in cited)
    if set(_NUMBER.findall(claim.text)) - set(_NUMBER.findall(f"{excerpts} {values}")):
        return 0.0
    if unsupported_judgement(claim.text, cited):
        return 0.0
    wanted = stem_set(claim.text)
    if not wanted:
        return 0.0
    return len(wanted & stem_set(excerpts)) / len(wanted)


def _unverified(
    kept: list[Claim], removed: list[Claim], evidence_by_id: dict[str, EvidenceItem]
) -> list[UnverifiedEvidence]:
    """Excerpts behind withheld claims that no surviving claim cites: still shown, as quotes."""
    cited = {evidence_id for claim in kept for evidence_id in claim.evidence_ids}
    shown: list[UnverifiedEvidence] = []
    per_question: dict[str, int] = {}
    seen: set[str] = set()
    for claim in removed:
        for evidence_id in claim.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if (
                item is None
                or item.status != "accepted"
                or evidence_id in cited
                or evidence_id in seen
                or item.claim_fit == ClaimFit.INSUFFICIENT
                or per_question.get(item.question_id, 0) >= _UNVERIFIED_PER_QUESTION
                or len(shown) >= _UNVERIFIED_MAX
            ):
                continue
            seen.add(evidence_id)
            per_question[item.question_id] = per_question.get(item.question_id, 0) + 1
            shown.append(
                UnverifiedEvidence(
                    evidence_id=evidence_id,
                    question_id=item.question_id,
                    source_id=item.source_id,
                    excerpt=item.excerpt,
                    reason=(claim.verification_notes or [""])[-1][:200],
                )
            )
    return shown


def label_strengths(
    plan: ResearchPlan,
    claims: tuple[Claim, ...] | list[Claim],
    evidence: list[EvidenceItem],
    sources: list[ResearchSource],
) -> None:
    """Give each verified claim the strength a reader should take from it, in place.

    verified: entailed, and backed by two independent sources or by the subject's own record
    stating a fact about itself. limited: entailed, but only partly supported or resting on
    dated evidence for a current question. single_source: entailed, one unofficial source.
    disputed: sources disagree. one_side_only: a comparison only one side's evidence speaks to.
    """
    evidence_by_id = {item.id: item for item in evidence}
    by_source = {source.id: source for source in sources}
    for claim in claims:
        cited = _cited(claim, evidence_by_id)
        keys: set[str] = set()
        official = False
        wanted = stem_set(claim.text)
        for item in cited:
            source = by_source.get(item.source_id)
            if source is None:
                continue
            # Only a source whose own excerpt carries the claim corroborates it; a citation
            # added beside it (a related passage from another page) does not.
            if wanted and len(wanted & stem_set(item.excerpt)) / len(wanted) < SUPPORTS_ALONE:
                continue
            assessment = source.assessment
            if assessment is not None and assessment.provenance == Provenance.D:
                continue
            keys.add(source.independence_key)
            if (
                assessment is not None
                and assessment.role == SourceRole.ISSUER_RECORD
                and assessment.provenance in (Provenance.A, Provenance.B)
                and (not claim.entity_ids or set(claim.entity_ids) & set(source.official_for))
                and not unsupported_judgement(claim.text, [item])
            ):
                official = True
        target = plan.target(claim.question_id) if claim.question_id else None
        stale = bool(cited) and all(i.freshness_fit == FreshnessFit.STALE for i in cited) and (
            target is not None and target.profile.recency.kind == RecencyKind.CURRENT
        )
        covered = {entity_id for item in cited for entity_id in _about(plan, item)}
        if claim.support in (
            SupportStatus.CONFLICTING_EVIDENCE,
            SupportStatus.INHERENTLY_UNCERTAIN,
        ):
            claim.strength = "disputed"
        elif claim.claim_type in _DERIVED_TYPES and len(covered & set(claim.entity_ids)) < 2:
            claim.strength = "one_side_only"
        elif stale or claim.support != SupportStatus.SUPPORTED:
            claim.strength = "limited"
        elif len(keys) >= 2 or official:
            claim.strength = "verified"
        else:
            claim.strength = "single_source"


def _deterministic_checks(
    plan: ResearchPlan, claim: Claim, evidence_by_id: dict[str, EvidenceItem]
) -> str | None:
    _keep_named_entities(plan, claim)
    _refile(plan, claim, evidence_by_id)
    cited: list[EvidenceItem] = []
    for evidence_id in claim.evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None or item.status != "accepted":
            claim.verification_notes.append(
                f"dropped citation {evidence_id}: not accepted evidence"
            )
            continue
        if claim.question_id and not _same_target_family(plan, item.question_id, claim.question_id):
            claim.verification_notes.append(
                f"dropped citation {evidence_id}: evidence for another question"
            )
            continue
        if item.claim_fit == ClaimFit.INSUFFICIENT:
            claim.verification_notes.append(
                f"dropped citation {evidence_id}: its source cannot support a finding"
            )
            continue
        target = plan.target(item.question_id)
        if item.superseded_by and (
            target is None or target.profile.recency.kind == RecencyKind.CURRENT
        ):
            claim.verification_notes.append(
                f"dropped citation {evidence_id}: a newer statement has replaced it"
            )
            continue
        cited.append(item)
    claim.evidence_ids = [item.id for item in cited]
    if any(item.attribution == "inferred" for item in cited):
        claim.verification_notes.append(_INFERRED_NOTE)
    if not cited:
        return "no accepted evidence supports this claim"

    if claim.polarity == Polarity.NEGATES or claim.claim_type == ClaimType.NEGATIVE:
        if not any(item.polarity == Polarity.NEGATES for item in cited):
            return (
                "absence of evidence is not evidence of absence: nothing cited states the negative"
            )

    cited = _same_scope_evidence(claim, cited)
    claim.evidence_ids = [item.id for item in cited]
    if not cited:
        return "cited evidence describes different scopes, so none of it supports this claim"

    if _states_unsupported_numbers(claim, cited):
        claim.verification_notes.append(FIGURE_NOTE)
    if _generalises_from_too_few_cases(plan, claim, cited):
        claim.verification_notes.append(GENERALISED_NOTE)

    covered = {entity_id for item in cited for entity_id in _about(plan, item)}
    missing = [entity_id for entity_id in claim.entity_ids if entity_id not in covered]
    if missing:
        names = ", ".join(plan.entity(e).name if plan.entity(e) else e for e in missing)
        if claim.claim_type in (ClaimType.COMPARISON, ClaimType.TRADEOFF):
            return f"comparison lacks evidence about {names}"
        kept = [entity_id for entity_id in claim.entity_ids if entity_id in covered]
        if not kept:
            return f"cited evidence is not about {names}"
        # A fact tagged with an entity its evidence is not about, beside one it is: the extra
        # tag is a writer's slip (a small model tags every entity of a comparative question),
        # not a reason to lose a supported fact. The tag comes off; if the text itself asserts
        # something about that entity, the unstated-entity check below still removes it.
        claim.entity_ids = kept
        claim.verification_notes.append(
            f"tagged {names}, which its cited evidence is not about; that tag was removed"
        )
    unstated = _unstated_entities(plan, claim, cited)
    if unstated:
        names = ", ".join(entity.name for entity in unstated)
        return f"names {names}, which none of its cited evidence states"

    support = (
        SupportStatus.SUPPORTED
        if any(item.support == Support.DIRECT for item in cited)
        else SupportStatus.PARTIALLY_SUPPORTED
    )
    if support == SupportStatus.PARTIALLY_SUPPORTED:
        claim.verification_notes.append("cited evidence is only partial")
    if all(item.claim_fit == ClaimFit.CONTEXT for item in cited):
        # Context can inform a claim but never settle it: a party to the question, another
        # scope, or outdated evidence. Such a claim is narrowed to what its evidence is
        # (what that party said, at that date) or it is not reported at all.
        support = SupportStatus.PARTIALLY_SUPPORTED
        claim.verification_notes.append(CONTEXT_ONLY_NOTE)
    if (
        plan.understanding.time_sensitivity != TimeSensitivity.LOW
        and all(item.freshness_fit == FreshnessFit.STALE for item in cited)
        and support == SupportStatus.SUPPORTED
    ):
        support = SupportStatus.PARTIALLY_SUPPORTED
        claim.verification_notes.append("all cited evidence may be outdated")
    in_conflict, claim.opposing_evidence_ids, settled = _opposition(cited)
    if claim.claim_type == ClaimType.UNCERTAINTY:
        support = (
            SupportStatus.CONFLICTING_EVIDENCE
            if in_conflict
            else SupportStatus.INHERENTLY_UNCERTAIN
        )
    elif in_conflict:
        if not settled:
            claim._support_before_conflict = support
        support = SupportStatus.CONFLICTING_EVIDENCE
        claim.verification_notes.append(CONFLICT_NOTE)
    claim.support = support
    return None


def _opposition(cited: list[EvidenceItem]) -> tuple[bool, list[str], bool]:
    """Whether a claim's own evidence is contradicted, by which items, and whether that is settled.

    Only an item's own unexplained opponents count: sharing a conflict group with some other
    item says nothing about this one. The conflict is settled, beyond any audit's reach, when
    the claim cites both sides (it is stating the disagreement, whatever it says) or rests on
    evidence recorded before opponents were (``None``), whose opponents are unknown.
    """
    cited_ids = {item.id for item in cited}
    opposing: list[str] = []
    settled = False
    for item in cited:
        if item.contradicted_by is None:
            settled = settled or item.contradiction_group is not None
            continue
        opposing.extend(other for other in item.contradicted_by if other not in cited_ids)
        settled = settled or any(other in cited_ids for other in item.contradicted_by)
    opposing = list(dict.fromkeys(opposing))
    return bool(opposing) or settled, opposing, settled


def _auditable_conflict(claim: Claim) -> bool:
    """A conflict label the audit may lift: one resting only on named opposing evidence."""
    return (
        claim.support == SupportStatus.CONFLICTING_EVIDENCE
        and claim.claim_type != ClaimType.UNCERTAINTY
        and claim._support_before_conflict is not None
        and bool(claim.opposing_evidence_ids)
    )


#: Independent cases a claim about a population, practice or trend has to rest on before it
#: can be stated of them all rather than of the cases seen.
CASES_FOR_A_GENERAL_CLAIM = 3
#: A figure a reader would take away: a standalone quantity, not the digits inside a name, an
#: identifier or a version number, which belong to the thing rather than measuring it.
_NUMBER = re.compile(r"(?<![\w.-])\d+(?:[.,]\d+)?(?![\w-])")


def _generalises_from_too_few_cases(
    plan: ResearchPlan, claim: Claim, cited: list[EvidenceItem]
) -> bool:
    """Whether a claim about things in general rests on too few of them.

    What happened in one place, one trial, one version or one deployment is evidence about
    that case. Saying it of the population needs either evidence that speaks for the
    population, or enough separate cases to stand for it; otherwise the claim has to name the
    case it came from.
    """
    target = plan.target(claim.question_id) if claim.question_id else None
    if target is None or target.profile.claim_scope != ClaimScope.GENERAL:
        return False
    if any(item.evidence_scope == ClaimScope.GENERAL for item in cited):
        return False
    case_items = [
        item
        for item in cited
        if item.evidence_scope in (ClaimScope.INSTANCE, ClaimScope.RECORD) or item.single_report
    ]
    if not case_items:
        return False
    # Different publishers do not identify different cases. Unlabelled case evidence
    # neither establishes breadth nor qualifies a claim to a named case.
    cases = {compact(item.scope) for item in case_items if scope_tokens(item.scope)}
    if len(cases) >= CASES_FOR_A_GENERAL_CLAIM:
        return False
    # A claim that says which case it is about is not a generalisation.
    spoken = token_set(claim.text)
    return not all(scope_tokens(item.scope) & spoken for item in case_items)


def _states_unsupported_numbers(claim: Claim, cited: list[EvidenceItem]) -> bool:
    """Whether the claim gives a figure its evidence does not.

    A number is the part of a statement a reader takes away and repeats, so it is held to the
    excerpt: every figure the claim states has to appear in what was cited for it.
    """
    stated = set(_NUMBER.findall(claim.text))
    if not stated:
        return False
    available = set()
    for item in cited:
        available |= set(_NUMBER.findall(f"{item.excerpt} {item.value or ''}"))
    return bool(stated - available)


def _same_scope_evidence(claim: Claim, cited: list[EvidenceItem]) -> list[EvidenceItem]:
    """Evidence that can stand behind one claim: one scope, or a claim that names them all.

    Evidence carries the scope its source stated: a place, a population, a version, a
    period, a set of conditions. Figures gathered under scopes that describe different
    subjects do not add up to one statement, so unless the claim says which is which, the
    claim keeps the scope most of its evidence shares and drops the rest.
    """
    scoped = [item for item in cited if scope_tokens(item.scope)]
    if len(scoped) < 2:
        return cited
    groups: list[tuple[str, list[EvidenceItem]]] = []
    for item in scoped:
        for scope, members in groups:
            if not scope_conflict(scope, item.scope):
                members.append(item)
                break
        else:
            groups.append((item.scope or "", [item]))
    if len(groups) < 2:
        return cited
    spoken = token_set(claim.text)
    if all(scope_tokens(scope) & spoken for scope, _ in groups):
        # The claim itself distinguishes the scopes, so the comparison is the reader's to make.
        claim.verification_notes.append(
            "cites evidence from several scopes, each named in the claim"
        )
        return cited
    order = {item.id: index for index, item in enumerate(cited)}
    keep = max(groups, key=lambda group: (len(group[1]), -order[group[1][0].id]))
    dropped = [item for scope, members in groups if scope != keep[0] for item in members]
    claim.verification_notes.append(
        "dropped citation(s) "
        + ", ".join(item.id for item in dropped)
        + f": {SCOPE_NOTE} {keep[0]!r}"
    )
    keep_ids = {item.id for item in keep[1]}
    return [item for item in cited if item.id in keep_ids or not scope_tokens(item.scope)]


def _unstated_entities(plan: ResearchPlan, claim: Claim, cited: list[EvidenceItem]) -> list[Entity]:
    """Entities the claim names that none of its evidence is about or names.

    A writer that knows the question can carry its subject into a sentence whose evidence never
    said what it was about. The claim then reads as an answer the sources did not give, and
    the entailment audit, reading the claim beside the excerpt, is the check most likely to
    let that pass. Code can see it: the name is in the claim and in none of the evidence.
    """
    unstated: list[Entity] = []
    for entity in plan.understanding.entities:
        others = [
            name
            for other in plan.understanding.entities
            if other.id != entity.id
            for name in other.names()
        ]
        forms = name_forms(entity.names())
        if names_entity(claim.text, entity.names(), others) is not True and not mentions_any(
            claim.text, forms
        ):
            continue
        if any(
            entity.id in item.entity_ids
            or names_entity(item.excerpt, entity.names(), others) is not False
            or mentions_any(item.excerpt, forms)
            for item in cited
        ):
            continue
        unstated.append(entity)
    return unstated


def _keep_named_entities(plan: ResearchPlan, claim: Claim) -> None:
    """A claim is about the entities its own text names.

    Synthesis sometimes tags every entity of a comparative question on a sentence that
    mentions only one of them. Tags the text does not name come off, and a comparison left
    with one entity is kept as a fact about it. Nothing changes when the text names none of
    the tagged entities (a pronoun or a generic reference) or a name is too short to check.
    """
    if claim.claim_type == ClaimType.RECOMMENDATION or len(claim.entity_ids) < 2:
        return
    verdicts: dict[str, bool | None] = {}
    for entity_id in claim.entity_ids:
        entity = plan.entity(entity_id)
        if entity is None:
            return
        others = [
            name
            for other in plan.understanding.entities
            if other.id != entity_id
            for name in other.names()
        ]
        verdicts[entity_id] = names_entity(claim.text, entity.names(), others)
    unnamed = [entity_id for entity_id, named in verdicts.items() if named is False]
    if not unnamed or not any(named is True for named in verdicts.values()):
        return
    claim.entity_ids = [entity_id for entity_id in claim.entity_ids if entity_id not in unnamed]
    names = ", ".join(entity.name for e in unnamed if (entity := plan.entity(e)) is not None)
    claim.verification_notes.append(f"the claim does not mention {names}; that tag was removed")
    if claim.claim_type == ClaimType.COMPARISON and len(claim.entity_ids) < 2:
        claim.claim_type = ClaimType.FACT
        claim.verification_notes.append("names only one entity, so it is kept as a fact")


def _recommendation_checks(plan: ResearchPlan, claim: Claim, kept: dict[str, Claim]) -> str | None:
    reasons = [
        kept[claim_id]
        for claim_id in claim.supporting_claim_ids
        if claim_id in kept and kept[claim_id].support == SupportStatus.SUPPORTED
    ]
    if not reasons:
        return "recommendation has no supported reasons"
    claim.supporting_claim_ids = [reason.id for reason in reasons]
    claim.evidence_ids = list(dict.fromkeys(e for reason in reasons for e in reason.evidence_ids))
    reason_entities = {entity_id for reason in reasons for entity_id in reason.entity_ids}
    if any(entity_id not in reason_entities for entity_id in claim.entity_ids):
        return "recommendation names an option its supporting claims do not cover"
    options = {
        entity_id
        for target in plan.top_targets()
        if target.tier == Tier.CORE
        for entity_id in target.subject_ids
    }
    if len(options) >= 2 and not claim.condition:
        supported_about = {
            entity_id
            for other in kept.values()
            if other.support == SupportStatus.SUPPORTED
            and other.claim_type != ClaimType.RECOMMENDATION
            for entity_id in other.entity_ids
        }
        if not options <= supported_about:
            return "unconditional recommendation from one-sided evidence"
    claim.support = SupportStatus.SUPPORTED
    return None


def _in_scope(plan: ResearchPlan, claim: Claim, policy: ResearchPolicy) -> bool:
    """Whether the mode checks this claim's entailment. Every mode checks every core claim."""
    if _INFERRED_NOTE in claim.verification_notes:
        return True
    if policy.entailment != "core_decisive" or claim.claim_type in _DECISIVE_TYPES:
        return True
    target = plan.target(claim.question_id)
    return bool(target and target.tier == Tier.CORE)


def _root(plan: ResearchPlan, target_id: str) -> str:
    target = plan.target(target_id)
    return target.parent_id if target is not None and target.parent_id else target_id


def _refile(plan: ResearchPlan, claim: Claim, evidence_by_id: dict[str, EvidenceItem]) -> None:
    """File a claim under the question its evidence answers, when the writer misfiled it.

    A small model files a comparison under one side's question, or a fact under the question
    next to it. The claim is not wrong for that, and dropping its citations as "evidence for
    another question" is how supported facts disappeared from reports.
    """
    cited = [
        item
        for e in claim.evidence_ids
        if (item := evidence_by_id.get(e)) is not None and item.status == "accepted"
    ]
    if not cited:
        return
    questions = list(dict.fromkeys(item.question_id for item in cited))
    roots = {_root(plan, question) for question in questions}
    if len(questions) > 1 and len(roots) == 1:
        target = next(iter(roots))
    elif claim.question_id and any(
        _same_target_family(plan, q, claim.question_id) for q in questions
    ):
        return
    else:
        target = max(questions, key=lambda q: sum(item.question_id == q for item in cited))
    if target != claim.question_id and plan.target(target) is not None:
        if claim.question_id:
            claim.verification_notes.append("refiled under the question its evidence answers")
        claim.question_id = target


def _same_target_family(plan: ResearchPlan, evidence_target: str, claim_target: str) -> bool:
    """Evidence filed under a target, its parent or its per-subject children bears on it."""
    if evidence_target == claim_target:
        return True
    evidence = plan.target(evidence_target)
    claim = plan.target(claim_target)
    return bool(
        (evidence and evidence.parent_id == claim_target)
        or (claim and claim.parent_id == evidence_target)
    )


def _restates_evidence(claim: Claim, evidence_by_id: dict[str, EvidenceItem]) -> bool:
    claim_tokens = token_set(claim.text)
    return any(
        jaccard(claim_tokens, token_set(evidence_by_id[e].claim)) >= 0.6
        for e in claim.evidence_ids
        if e in evidence_by_id
    )


def _entailment(
    plan: ResearchPlan,
    claims: list[Claim],
    evidence_by_id: dict[str, EvidenceItem],
    kept: dict[str, Claim],
    *,
    policy: ResearchPolicy,
    llm_factory: LLMFactory,
    llm_budget: LLMBudget,
    remove,
    settle,
) -> list[Claim]:
    """Check every claim against its excerpts. Returns the claims that say more than they state.

    A claim the model could not audit goes to ``settle``, which applies the wording check.
    """
    weakened: list[Claim] = []
    per_call = min(
        _AUDIT_BATCH,
        items_per_call(
            llm_budget.capability,
            output_tokens_per_item=_VERDICT_OUTPUT_TOKENS,
            overhead_tokens=80,
            ceiling=_AUDIT_BATCH,
        ),
    )
    for start in range(0, len(claims), per_call):
        batch = claims[start : start + per_call]
        if not llm_budget.can_spend(reserved=True):
            for claim in batch:
                settle(claim, "not audited by the model: no time or calls left")
            continue
        payload = {
            "claims": [
                {
                    "claim_id": claim.id,
                    "text": claim.text,
                    "claim_type": claim.claim_type.value,
                    "entities": [plan.entity(e).name for e in claim.entity_ids if plan.entity(e)],
                    "evidence": [
                        {
                            "about": [
                                entity.name
                                for entity_id in evidence_by_id[e].entity_ids
                                if (entity := plan.entity(entity_id)) is not None
                            ],
                            "excerpt": evidence_by_id[e].excerpt,
                        }
                        for e in claim.evidence_ids
                        if e in evidence_by_id
                    ][:8],
                    "reasons": [kept[c].text for c in claim.supporting_claim_ids if c in kept],
                    "condition": claim.condition,
                    **_opposing_payload(plan, claim, evidence_by_id),
                }
                for claim in batch
            ]
        }
        try:
            output = call_json(
                llm_factory,
                stage="entailment_audit",
                instructions=ENTAILMENT_INSTRUCTIONS,
                payload=payload,
                schema=_VerdictOutput,
                budget=llm_budget,
                num_predict=900,
                reserved=True,
            )
        except STAGE_FAILURES:
            for claim in batch:
                settle(claim, "the model audit failed")
            continue
        verdicts = {item.claim_id: item for item in output.results}
        for claim in batch:
            result = verdicts.get(claim.id)
            if result is not None and result.contradicted is False and _auditable_conflict(claim):
                # The opposing sources disagree about something else; the claim keeps the
                # support its own evidence gives it. No verdict, or any doubt, keeps the label.
                claim.support = claim._support_before_conflict
                claim.verification_notes.append(LIFTED_CONFLICT_NOTE)
            verdict = result.entailment.strip().lower() if result is not None else None
            if verdict == "full" and not (result is not None and result.overstated):
                continue
            if verdict == "none":
                remove(claim, "cited excerpts do not entail the claim")
            elif verdict in ("full", "partial"):
                weakened.append(claim)
            else:
                settle(claim, "the model audit returned no verdict")
    return weakened


def _opposing_payload(
    plan: ResearchPlan, claim: Claim, evidence_by_id: dict[str, EvidenceItem]
) -> dict:
    if not _auditable_conflict(claim):
        return {}
    return {
        "opposing": [
            {
                "about": [
                    entity.name
                    for entity_id in evidence_by_id[e].entity_ids
                    if (entity := plan.entity(entity_id)) is not None
                ],
                "excerpt": evidence_by_id[e].excerpt,
                "scope": evidence_by_id[e].scope,
            }
            for e in claim.opposing_evidence_ids
            if e in evidence_by_id
        ][:4]
    }


def _union_only(
    plan: ResearchPlan,
    claims: list[Claim],
    evidence_by_id: dict[str, EvidenceItem],
    *,
    policy: ResearchPolicy,
    llm_factory: LLMFactory,
    llm_budget: LLMBudget,
) -> list[Claim]:
    """Claims no single excerpt states, which hold only by adding several sources together.

    Such a claim is the writer's own inference, however well each part is cited. It is narrowed
    to what one source states, or withheld. A comparison whose sources state one side each is
    checked the same way: the comparison itself has to come from somewhere.
    """
    in_scope = []
    for claim in claims:
        sources = {evidence_by_id[e].source_id for e in claim.evidence_ids if e in evidence_by_id}
        if len(sources) < 2 or claim.support != SupportStatus.SUPPORTED:
            continue
        if policy.union_check or claim.claim_type in _DERIVED_TYPES:
            in_scope.append(claim)
    if not in_scope:
        return []
    # Each excerpt is offered on its own: if one states the claim, the claim is that source's.
    singles: list[tuple[str, Claim, EvidenceItem]] = []
    for claim in in_scope:
        for index, evidence_id in enumerate(claim.evidence_ids[:4]):
            item = evidence_by_id.get(evidence_id)
            if item is not None:
                singles.append((f"{claim.id}#{index}", claim, item))
    entailed: set[str] = set()
    per_call = min(
        _AUDIT_BATCH,
        items_per_call(
            llm_budget.capability,
            output_tokens_per_item=_VERDICT_OUTPUT_TOKENS,
            overhead_tokens=80,
            ceiling=_AUDIT_BATCH,
        ),
    )
    for start in range(0, len(singles), per_call):
        batch = singles[start : start + per_call]
        if not llm_budget.can_spend(reserved=True):
            return []  # unchecked is not the same as failed: the claims keep their citations
        payload = {
            "claims": [
                {
                    "claim_id": pair_id,
                    "text": claim.text,
                    "claim_type": claim.claim_type.value,
                    "entities": [plan.entity(e).name for e in claim.entity_ids if plan.entity(e)],
                    "evidence": [
                        {
                            "about": [
                                entity.name
                                for entity_id in item.entity_ids
                                if (entity := plan.entity(entity_id)) is not None
                            ],
                            "excerpt": item.excerpt,
                        }
                    ],
                }
                for pair_id, claim, item in batch
            ]
        }
        try:
            output = call_json(
                llm_factory,
                stage="entailment_audit",
                instructions=ENTAILMENT_INSTRUCTIONS,
                payload=payload,
                schema=_VerdictOutput,
                budget=llm_budget,
                num_predict=900,
                reserved=True,
            )
        except STAGE_FAILURES:
            return []
        for verdict in output.results:
            if verdict.entailment.strip().lower() == "full" and not verdict.overstated:
                entailed.add(verdict.claim_id.partition("#")[0])
    return [claim for claim in in_scope if claim.id not in entailed]


def _rewrite(
    plan: ResearchPlan,
    claims: list[Claim],
    evidence_by_id: dict[str, EvidenceItem],
    kept: dict[str, Claim],
    *,
    policy: ResearchPolicy,
    llm_factory: LLMFactory,
    llm_budget: LLMBudget,
    remove,
    settle,
) -> None:
    """Narrow claims their excerpts only partly support, then check them again.

    A claim is never reported as a partly supported answer: it is rewritten down to what the
    evidence states and re-verified. When it cannot be rewritten -- no time, the model failing
    or returning it unchanged -- the wording check decides: entailed by its excerpts' words it
    stands, otherwise its excerpts are shown as unverified evidence.
    """
    if not claims:
        return
    if not llm_budget.can_spend(reserved=True):
        for claim in claims:
            settle(claim, "could not be narrowed: no time or calls left")
        return
    payload = {
        "claims": [
            {
                "claim_id": claim.id,
                "text": claim.text,
                "claim_type": claim.claim_type.value,
                "excerpts": [
                    evidence_by_id[e].excerpt for e in claim.evidence_ids if e in evidence_by_id
                ][:8],
            }
            for claim in claims
        ]
    }
    try:
        output = call_json(
            llm_factory,
            stage="claim_rewrite",
            instructions=REWRITE_INSTRUCTIONS,
            payload=payload,
            schema=_RewriteOutput,
            budget=llm_budget,
            num_predict=1200,
            reserved=True,
        )
    except STAGE_FAILURES:
        for claim in claims:
            settle(claim, "could not be narrowed: the model failed")
        return
    texts = {item.claim_id: normalize_ws(item.text) for item in output.claims}
    narrowed: list[Claim] = []
    for claim in claims:
        text = texts.get(claim.id, "")
        if not text or len(text.split()) > 40:
            remove(claim, "cited excerpts support only part of the claim")
            continue
        if text.strip() == claim.text.strip():
            settle(claim, "the model returned it unchanged when asked to narrow it")
            continue
        claim.text = text
        claim.verification_notes.append(NARROWED_NOTE)
        _keep_named_entities(plan, claim)
        narrowed.append(claim)
    still_weak = _entailment(
        plan,
        narrowed,
        evidence_by_id,
        kept,
        policy=policy,
        llm_factory=llm_factory,
        llm_budget=llm_budget,
        remove=remove,
        settle=settle,
    )
    for claim in still_weak:
        if claim.id in {c.id for c in narrowed} and wording_entailment(
            claim, _cited(claim, evidence_by_id)
        ) >= WORDING_ENTAILMENT and not unsupported_judgement(
            claim.text, _cited(claim, evidence_by_id)
        ):
            claim.verification_notes.append("narrowed claim matches its excerpts' wording")
            _cap(claim, SupportStatus.PARTIALLY_SUPPORTED, "narrowed to its excerpts")
            continue
        remove(claim, "the narrowed claim is still not stated by the cited excerpts")


def _cap(claim: Claim, ceiling: SupportStatus, note: str) -> None:
    if claim.support not in _SUPPORT_ORDER or ceiling not in _SUPPORT_ORDER:
        return
    if _SUPPORT_ORDER.index(claim.support) < _SUPPORT_ORDER.index(ceiling):
        claim.support = ceiling
        claim.verification_notes.append(note)
