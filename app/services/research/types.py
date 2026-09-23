"""Typed models for Research Mode: budgets, plans, sources, evidence, coverage, claims, traces.

Everything here is topic-agnostic. Task types, question scopes, evidence needs and source
classes are generic research vocabulary; the entities, dimensions and questions of a run
come from the planner at runtime, never from this module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.services.search.types import PageLink


class DepthMode(StrEnum):
    """The research modes a user chooses between. See ``research.policy`` for what each buys."""

    QUICK = "quick"
    STANDARD = "standard"
    DEEP = "deep"

    @classmethod
    def _missing_(cls, value: object) -> DepthMode | None:
        # Runs saved, requested or scripted before the modes were consolidated used a fourth
        # "technical_deep" mode; it reads as Deep, which now carries its strict verification.
        if isinstance(value, str) and value.strip().lower() == "technical_deep":
            return cls.DEEP
        return None


class Tier(StrEnum):
    """How much an evidence target matters to the answer."""

    CORE = "core"
    SUPPORTING = "supporting"
    CONTEXT = "context"


class Intent(StrEnum):
    """What a research action is trying to do for a target."""

    DIRECT_ANSWER = "direct_answer"
    LOCATE_ORIGIN = "locate_origin"
    FOLLOW_LEAD = "follow_lead"
    ACCESSIBLE_COPY = "accessible_copy"
    CORROBORATE = "corroborate"
    RESOLVE_CONFLICT = "resolve_conflict"
    BROADEN = "broaden"


class NeedKind(StrEnum):
    """Something a target's evidence still lacks. Whether it is worth pursuing is decided by
    materiality and policy, never by the need's existence alone."""

    INSUFFICIENT = "insufficient"
    SUBJECT_IMBALANCE = "subject_imbalance"
    STALE = "stale"
    SCOPE_UNRESOLVED = "scope_unresolved"
    CONFLICT_MATERIAL = "conflict_material"
    CONFLICT_MINOR = "conflict_minor"
    PROVENANCE_UNCONFIRMED = "provenance_unconfirmed"
    UPGRADE_AVAILABLE = "upgrade_available"
    CORROBORATION_HELPFUL = "corroboration_helpful"


class Strength(StrEnum):
    MISSING = "missing"
    WEAK = "weak"
    MEDIUM = "medium"
    STRONG = "strong"


STRENGTH_ORDER: dict[Strength, int] = {
    Strength.MISSING: 0,
    Strength.WEAK: 1,
    Strength.MEDIUM: 2,
    Strength.STRONG: 3,
}


# --- Generic research vocabulary -------------------------------------------------------


class TaskType(StrEnum):
    FACTUAL_LOOKUP = "factual_lookup"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    RECOMMENDATION = "recommendation"
    TECHNICAL_INVESTIGATION = "technical_investigation"
    PRODUCT_EVALUATION = "product_evaluation"
    SCIENTIFIC_RESEARCH = "scientific_research"
    CURRENT_EVENTS = "current_events"
    HISTORICAL_RESEARCH = "historical_research"
    TROUBLESHOOTING = "troubleshooting"
    MARKET_RESEARCH = "market_research"
    ENTITY_RESEARCH = "entity_research"
    OPEN_EXPLORATION = "open_exploration"


class TimeSensitivity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Evidence older than this no longer counts as current for the plan's time sensitivity.
MAX_EVIDENCE_AGE_DAYS: dict[TimeSensitivity, int | None] = {
    TimeSensitivity.HIGH: 400,
    TimeSensitivity.MEDIUM: 3 * 365,
    TimeSensitivity.LOW: None,
}


class QuestionScope(StrEnum):
    ENTITY_SPECIFIC = "entity_specific"
    MULTI_ENTITY = "multi_entity"
    COMPARATIVE = "comparative"
    RELATIONAL = "relational"
    ECOSYSTEM = "ecosystem"
    CAUSAL = "causal"
    HISTORICAL = "historical"
    MECHANISM = "mechanism"
    ENTITY_INDEPENDENT = "entity_independent"


JOINT_SCOPES = frozenset(
    {
        QuestionScope.COMPARATIVE,
        QuestionScope.RELATIONAL,
        QuestionScope.CAUSAL,
        QuestionScope.ECOSYSTEM,
    }
)


class Importance(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"


class EvidenceNeed(StrEnum):
    OFFICIAL_FACT = "official_fact"
    SPECIFICATION = "specification"
    PRICING_OR_AVAILABILITY = "pricing_or_availability"
    DOCUMENTATION = "documentation"
    SCIENTIFIC = "scientific"
    LEGAL_OR_REGULATORY = "legal_or_regulatory"
    NEWS = "news"
    DATA = "data"
    INDEPENDENT_ANALYSIS = "independent_analysis"
    USER_EXPERIENCE = "user_experience"
    HISTORICAL_RECORD = "historical_record"
    EXPERT_OPINION = "expert_opinion"


class SearchStrategy(StrEnum):
    INITIAL = "initial"
    TARGETED = "targeted"
    ALTERNATE_SOURCE_CLASS = "alternate_source_class"
    BROADER_TERMS = "broader_terms"
    ALTERNATE_EVIDENCE_TYPE = "alternate_evidence_type"
    CONFLICT_RESOLUTION = "conflict_resolution"


class SourceClass(StrEnum):
    PRIMARY_OFFICIAL = "primary_official"
    PRIMARY_DATA = "primary_data"
    ACADEMIC = "academic"
    GOVERNMENT = "government"
    DOCUMENTATION = "documentation"
    REPUTABLE_SECONDARY = "reputable_secondary"
    EXPERT_ANALYSIS = "expert_analysis"
    COMMUNITY = "community"
    AGGREGATOR = "aggregator"
    LOW_QUALITY = "low_quality"
    UNKNOWN = "unknown"


class Suitability(StrEnum):
    PREFERRED = "preferred"
    ACCEPTABLE = "acceptable"
    WEAK = "weak"


class RejectionReason(StrEnum):
    IRRELEVANT = "irrelevant"
    DUPLICATE = "duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    POOR_EXTRACTION = "poor_extraction"
    LOW_AUTHORITY = "low_authority"
    SPAM = "spam"
    INSUFFICIENT_CONTENT = "insufficient_content"
    WRONG_ENTITY = "wrong_entity"
    STALE = "stale"
    FETCH_FAILURE = "fetch_failure"
    UNSUPPORTED_FORMAT = "unsupported_format"
    APP_CHROME = "app_chrome"
    #: A keyword list, tag cloud, navigation trail, label/value metadata or title: text that
    #: names things without stating anything about them.
    NON_PROPOSITIONAL = "non_propositional"
    UNGROUNDED_EXCERPT = "ungrounded_excerpt"
    INVALID_REFERENCE = "invalid_reference"
    NO_SUPPORT = "no_support"
    BUDGET = "budget"


class StopReason(StrEnum):
    COVERAGE_SUFFICIENT = "coverage_sufficient"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SEARCH_UNAVAILABLE = "search_unavailable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    USER_CANCELLED = "user_cancelled"
    FAILED = "failed"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    INSUFFICIENT_RESEARCH = "insufficient_research"
    INHERENTLY_UNCERTAIN = "inherently_uncertain"
    UNVERIFIABLE = "unverifiable"


class Relevance(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class Support(StrEnum):
    DIRECT = "direct"
    PARTIAL = "partial"
    NONE = "none"


class Polarity(StrEnum):
    AFFIRMS = "affirms"
    NEGATES = "negates"


class FreshnessFit(StrEnum):
    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


class ClaimType(StrEnum):
    FACT = "fact"
    COMPARISON = "comparison"
    TRADEOFF = "tradeoff"
    RECOMMENDATION = "recommendation"
    NEGATIVE = "negative"
    MECHANISM = "mechanism"
    LIMITATION = "limitation"
    UNCERTAINTY = "uncertainty"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AuthorityKind(StrEnum):
    """What kind of authority would settle a target, independent of topic."""

    #: What an organisation, person or project itself said, did, specified or published.
    ISSUER = "issuer"
    #: A figure from data or statistics.
    MEASUREMENT = "measurement"
    #: What research or expert bodies have established overall.
    CONSENSUS = "consensus"
    #: How good, reliable or suitable something is, as judged by others.
    EVALUATIVE = "evaluative"


class ClaimScope(StrEnum):
    """What a claim, or the evidence for it, is about."""

    #: What a specific document, issuer or study states or reports.
    RECORD = "record"
    #: A specific event, case or individual report.
    INSTANCE = "instance"
    #: A generalisation over a population, product, practice or trend.
    GENERAL = "general"


class OriginHint(StrEnum):
    """The planner's guess whether an originating record exists. A hint, never a constraint."""

    LIKELY = "likely"
    UNKNOWN = "unknown"
    UNLIKELY = "unlikely"


class OriginatorKind(StrEnum):
    """Who would have published the record that settles a target, as a kind of body.

    This is how research looks for an original: not by knowing a site, but by knowing what
    kind of body publishes this sort of record and searching in those terms.
    """

    #: A national or regional arm of government.
    GOVERNMENT_AGENCY = "government_agency"
    #: A city, municipal or local authority.
    CITY_AUTHORITY = "city_authority"
    #: A body that oversees an industry or activity.
    REGULATOR = "regulator"
    #: A body that publishes official statistics.
    STATISTICAL_OFFICE = "statistical_office"
    #: A body that runs a service or network.
    OPERATING_AUTHORITY = "operating_authority"
    #: A body that sets standards or specifications.
    STANDARDS_BODY = "standards_body"
    #: A court, legislature or the record of their proceedings.
    COURT_OR_LEGISLATURE = "court_or_legislature"
    #: A university, institute or other research institution.
    ACADEMIC_INSTITUTION = "academic_institution"
    #: The authors of the study or paper itself.
    RESEARCH_AUTHORS = "research_authors"
    #: The company or publisher of the product in question.
    COMPANY_PUBLISHER = "company_publisher"
    #: The people who maintain the project.
    PROJECT_MAINTAINERS = "project_maintainers"
    #: A news or reporting organisation of record.
    NEWS_ORGANISATION = "news_organisation"
    #: Unknown, or none of these.
    OTHER = "other"


class OutcomeRole(StrEnum):
    """Which side of a question a target answers.

    A question that asks whether something worked without causing harm has two halves, and an
    answer to one is not an answer to the other.
    """

    #: The effect the question asks whether something achieved.
    BENEFIT = "benefit"
    #: The harm or side effect the question asks whether something avoided.
    HARM = "harm"
    #: Neither: most targets are simply questions of fact.
    NEITHER = "neither"


class RecordForm(StrEnum):
    PUBLICATION = "publication"
    FILING = "filing"
    LEGAL_TEXT = "legal_text"
    DATASET = "dataset"
    DOCUMENTATION = "documentation"
    ANNOUNCEMENT = "announcement"
    RELEASE_NOTES = "release_notes"
    OTHER = "other"


class RecencyKind(StrEnum):
    #: Settled; old evidence is as good as new.
    TIMELESS = "timeless"
    #: About a specific time or event; evidence must describe that time.
    AS_OF_EVENT = "as_of_event"
    #: Depends on the present state of things; evidence must be recent.
    CURRENT = "current"


class ErrorProne(StrEnum):
    """Why even an authoritative single source might be wrong or disputed."""

    NONE = "none"
    ESTIMATE = "estimate"
    FORECAST = "forecast"
    SELF_REPORTED = "self_reported"


class TargetForm(StrEnum):
    #: About one subject, or none.
    SINGLE = "single"
    #: The same question asked of each subject; answered per subject.
    PER_SUBJECT = "per_subject"
    #: Answerable only by comparing or relating the subjects.
    JOINT = "joint"


# --- Plan ------------------------------------------------------------------------------


class Entity(BaseModel):
    id: str
    name: str
    type: str = ""
    aliases: list[str] = Field(default_factory=list)

    def names(self) -> list[str]:
        return [name for name in dict.fromkeys([self.name, *self.aliases]) if name.strip()]


class Constraint(BaseModel):
    id: str
    text: str


class DecisionCriterion(BaseModel):
    id: str
    text: str


class TaskUnderstanding(BaseModel):
    task_type: TaskType = TaskType.OPEN_EXPLORATION
    answer_shape: str = ""
    time_sensitivity: TimeSensitivity = TimeSensitivity.MEDIUM
    entities: list[Entity] = Field(default_factory=list)
    constraints: list[Constraint] = Field(default_factory=list)
    decision_criteria: list[DecisionCriterion] = Field(default_factory=list)


class Dimension(BaseModel):
    id: str
    name: str
    description: str = ""
    justification_refs: list[str] = Field(default_factory=list)


class ResearchQuestion(BaseModel):
    id: str
    text: str
    dimension_id: str
    importance: Importance = Importance.SUPPORTING
    scope: QuestionScope = QuestionScope.ENTITY_INDEPENDENT
    entity_ids: list[str] = Field(default_factory=list)
    decompose_by_entity: bool = False
    evidence_needs: list[EvidenceNeed] = Field(default_factory=list)
    constraint_ids: list[str] = Field(default_factory=list)
    criterion_ids: list[str] = Field(default_factory=list)


class Recency(BaseModel):
    kind: RecencyKind = RecencyKind.TIMELESS
    #: For current targets: how old evidence may be and still describe the present.
    max_age_days: int | None = None


class EvidenceProfile(BaseModel):
    """What the best evidence for a target would look like. Planner hints, checked by evidence."""

    authority_kind: AuthorityKind = AuthorityKind.ISSUER
    claim_scope: ClaimScope = ClaimScope.RECORD
    origin_hint: OriginHint = OriginHint.UNKNOWN
    record_form: RecordForm = RecordForm.OTHER
    #: The kind of body that would have published the record, used to search for it.
    originator: OriginatorKind = OriginatorKind.OTHER
    recency: Recency = Field(default_factory=Recency)
    error_prone: ErrorProne = ErrorProne.NONE
    #: Which half of a "did it work without harm" question this target answers.
    outcome_role: OutcomeRole = OutcomeRole.NEITHER


class TargetHypotheses(BaseModel):
    """The planner's guesses, used only to word searches.

    They are never evidence and never shown to the evidence judge.
    """

    likely_issuers: list[str] = Field(default_factory=list)
    record_title_words: list[str] = Field(default_factory=list)
    expected_answer: str = ""


class EvidenceTarget(BaseModel):
    """Something the research has to establish, and what evidence would establish it."""

    id: str
    text: str
    dimension_id: str
    tier: Tier = Tier.SUPPORTING
    form: TargetForm = TargetForm.SINGLE
    subject_ids: list[str] = Field(default_factory=list)
    #: Set on the per-subject children of a per-subject or joint target.
    parent_id: str | None = None
    scope_note: str = ""
    profile: EvidenceProfile = Field(default_factory=EvidenceProfile)
    hypotheses: TargetHypotheses = Field(default_factory=TargetHypotheses)
    constraint_ids: list[str] = Field(default_factory=list)
    criterion_ids: list[str] = Field(default_factory=list)


class PlanComplexity(BaseModel):
    """How the question is shaped, for deciding how research effort is spread."""

    #: Top-level target ids grouped so targets in different groups share no subject and no
    #: dimension: research on one group cannot answer another.
    separable_groups: list[list[str]] = Field(default_factory=list)
    contested_likely: bool = False
    core_count: int = 0


class SearchTask(BaseModel):
    id: str
    question_id: str
    entity_ids: list[str] = Field(default_factory=list)
    purpose: EvidenceNeed | None = None
    strategy: SearchStrategy = SearchStrategy.INITIAL
    query: str
    round: int = 1
    gap_id: str | None = None


class ResearchDecomposition(BaseModel):
    dimensions: list[Dimension] = Field(default_factory=list)
    questions: list[ResearchQuestion] = Field(default_factory=list)
    search_tasks: list[SearchTask] = Field(default_factory=list)


class PlanIssue(BaseModel):
    code: str
    detail: str = ""
    repaired: bool = False


class ResearchPlan(BaseModel):
    query: str
    depth: DepthMode
    understanding: TaskUnderstanding
    decomposition: ResearchDecomposition
    targets: list[EvidenceTarget] = Field(default_factory=list)
    complexity: PlanComplexity = Field(default_factory=PlanComplexity)
    issues: list[PlanIssue] = Field(default_factory=list)

    def target(self, target_id: str | None) -> EvidenceTarget | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def top_targets(self) -> list[EvidenceTarget]:
        return [t for t in self.targets if t.parent_id is None]

    def children(self, target_id: str) -> list[EvidenceTarget]:
        return [t for t in self.targets if t.parent_id == target_id]

    def entity(self, entity_id: str) -> Entity | None:
        return next((e for e in self.understanding.entities if e.id == entity_id), None)

    def question(self, question_id: str | None) -> ResearchQuestion | None:
        return next((q for q in self.decomposition.questions if q.id == question_id), None)

    def dimension(self, dimension_id: str | None) -> Dimension | None:
        return next((d for d in self.decomposition.dimensions if d.id == dimension_id), None)

    def core_questions(self) -> list[ResearchQuestion]:
        return [q for q in self.decomposition.questions if q.importance == Importance.CORE]

    @property
    def max_evidence_age_days(self) -> int | None:
        return MAX_EVIDENCE_AGE_DAYS[self.understanding.time_sensitivity]


# --- Sources, chunks, evidence ---------------------------------------------------------


class SourceRole(StrEnum):
    """What kind of publisher a source is. Proposed by the judge, confirmed by signals."""

    #: A source publishing its own record, statement, data, product documentation or study.
    ISSUER_RECORD = "issuer_record"
    INSTITUTIONAL = "institutional"
    INDEPENDENT_EXPERT = "independent_expert"
    REPORTING = "reporting"
    REFERENCE = "reference"
    COMMUNITY = "community"
    AGGREGATOR = "aggregator"
    PROMOTIONAL = "promotional"
    UNKNOWN = "unknown"


class Provenance(StrEnum):
    """How trustworthy a source's provenance is. Independent of any claim."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class ClaimFit(StrEnum):
    """What one evidence item can do for its target's claim."""

    ESTABLISHES = "establishes"
    SUPPORTS = "supports"
    CONTEXT = "context"
    INSUFFICIENT = "insufficient"


class SourceAssessment(BaseModel):
    """What kind of source this is, and how its provenance was established."""

    role: SourceRole = SourceRole.UNKNOWN
    #: The issuer the judge says published the source, in the source's own words.
    issuer: str = ""
    review_status: str = ""
    #: Stated by the page about itself: a copy or lookalike can state them too.
    self_asserted: list[str] = Field(default_factory=list)
    #: Established by other, independent sources read in the same run.
    corroborated: list[str] = Field(default_factory=list)
    negative: list[str] = Field(default_factory=list)
    provenance: Provenance = Provenance.C
    #: The source id of the original, when this source is a copy of one.
    copy_of: str | None = None
    notes: list[str] = Field(default_factory=list)


class LeadKind(StrEnum):
    #: A grounded citation or link in a fetched page.
    CITATION = "citation"
    #: A search result that itself looks like the originating record.
    SEARCH_RESULT = "search_result"


class LeadStatus(StrEnum):
    OPEN = "open"
    FETCHED = "fetched"
    UNREADABLE = "unreadable"
    REJECTED = "rejected"


class OriginLead(BaseModel):
    """A pointer to a probable originating record, observed during research, never guessed."""

    id: str
    target_id: str
    kind: LeadKind = LeadKind.CITATION
    from_source_id: str | None = None
    from_action_id: str | None = None
    #: The independence group of the page or search that revealed the lead.
    independent_of: str = ""
    grounding_sentence_ids: list[int] = Field(default_factory=list)
    title: str = ""
    issuer: str = ""
    year: str = ""
    href: str | None = None
    status: LeadStatus = LeadStatus.OPEN


class ResearchSource(BaseModel):
    id: str
    url: str
    canonical_url: str
    host: str
    host_group: str
    title: str = ""
    snippet: str = ""
    site_name: str = ""
    organization_names: list[str] = Field(default_factory=list)
    schema_types: list[str] = Field(default_factory=list)
    scholarly: bool = False
    published_date: str | None = None
    modified_date: str | None = None
    author: str = ""
    provider: str = ""
    search_rank: int | None = None
    discovered_by_task_ids: list[str] = Field(default_factory=list)
    fetch_outcome: Literal["fetched", "cached", "failed"] = "failed"
    fetch_error: str | None = None
    http_status: int | None = None
    extraction_quality: float = 0.0
    content_words: int = 0
    duplicate_of: str | None = None
    source_class: SourceClass = SourceClass.UNKNOWN
    official_for: list[str] = Field(default_factory=list)
    class_signals: list[str] = Field(default_factory=list)
    rejection_reason: RejectionReason | None = None
    used: bool = False
    document_kind: Literal["html", "pdf", "text"] = "html"
    #: The canonical URL the page itself declares, when it declares one.
    declared_canonical: str | None = None
    links: list[PageLink] = Field(default_factory=list)
    citation_meta: dict[str, str] = Field(default_factory=dict)
    assessment: SourceAssessment | None = None

    @property
    def reviewed(self) -> bool:
        return self.fetch_outcome in ("fetched", "cached")

    @property
    def independence_key(self) -> str:
        return self.duplicate_of or self.host_group


class Chunk(BaseModel):
    id: str
    source_id: str
    text: str
    heading_path: list[str] = Field(default_factory=list)
    block_start: int = 0
    block_end: int = 0
    text_hash: str = ""


class EvidenceItem(BaseModel):
    id: str
    chunk_id: str
    source_id: str
    question_id: str
    entity_ids: list[str] = Field(default_factory=list)
    # Normalized statement of the fact; the excerpt is the verbatim grounding for it.
    claim: str
    excerpt: str
    polarity: Polarity = Polarity.AFFIRMS
    # "inferred" when the chunk never names an entity the judge attributed the fact to.
    attribution: Literal["stated", "inferred"] = "stated"
    # Optional structured-fact slots so evidence can evolve toward (subject, predicate,
    # value) facts without a migration.
    subject: str | None = None
    attribute: str | None = None
    value: str | None = None
    unit: str | None = None
    scope: str | None = None
    qualifiers: list[str] = Field(default_factory=list)
    as_of: str | None = None
    relevance: Relevance = Relevance.NONE
    support: Support = Support.NONE
    suitability: Suitability = Suitability.WEAK
    source_class: SourceClass = SourceClass.UNKNOWN
    extraction_quality: float = 0.0
    freshness_fit: FreshnessFit = FreshnessFit.UNKNOWN
    host_group: str = ""
    duplicate_of: str | None = None
    contradiction_group: str | None = None
    #: Accepted items that state the opposite of this one about the same property, subject and
    #: scope, in a disagreement nothing has explained. None until conflicts are classified
    #: (rows saved before this was recorded), when group membership is all that is known.
    contradicted_by: list[str] | None = None
    status: Literal["accepted", "rejected"] = "rejected"
    rejection_reason: RejectionReason | None = None
    # Ranking only. Acceptance is categorical and never decided by this number.
    priority_score: float = 0.0
    round: int = 1
    #: The chunk sentences the excerpt was rebuilt from, when the judge cited sentences.
    sentence_ids: list[int] = Field(default_factory=list)
    #: What the excerpt itself speaks about: a record, one instance, or a generalisation.
    evidence_scope: ClaimScope | None = None
    #: The judge found the excerpt to be one person's or one case's report.
    single_report: bool = False
    #: The excerpt names how a general finding was reached (sample, test, source data).
    method_stated: bool = False
    interested_party: bool = False
    claim_fit: ClaimFit | None = None
    lead_ids: list[str] = Field(default_factory=list)
    #: A newer item stating the same property for the same scope, on a target about the present.
    superseded_by: str | None = None


class ContradictionCause(StrEnum):
    """Why sources disagree, as far as code or the model could tell."""

    TIME = "time"
    SCOPE = "scope"
    VERSION = "version"
    DEFINITION = "definition"
    METHOD = "method"
    #: Both sides trace back to one upstream record: a copied error, not two views.
    SHARED_UPSTREAM = "shared_upstream"
    SUBSTANTIVE = "substantive"
    UNKNOWN = "unknown"


class ContradictionStatus(StrEnum):
    OPEN = "open"
    #: The disagreement has a cause (different times, scopes, versions) and both can be stated.
    EXPLAINED = "explained"
    #: Research could not explain it; the report states both sides.
    REPRESENTED = "represented"


class ContradictionGroup(BaseModel):
    id: str
    #: The evidence target the conflicting evidence was filed under.
    question_id: str
    entity_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    #: The pairs of items that actually disagree. A group joins every item linked through any
    #: pair, so two members need not disagree with each other at all.
    pairs: list[tuple[str, str]] = Field(default_factory=list)
    detail: str = ""
    detected_by: Literal["deterministic", "model"] = "deterministic"
    investigated: bool = False
    cause: ContradictionCause = ContradictionCause.UNKNOWN
    status: ContradictionStatus = ContradictionStatus.OPEN
    #: Research actions spent trying to resolve it.
    intents_spent: int = 0


class Sufficiency(StrEnum):
    ESTABLISHED = "established"
    #: A general claim supported only by several independent individual reports.
    HEDGED = "hedged"
    INSUFFICIENT = "insufficient"


class Basis(StrEnum):
    DECISIVE = "decisive"
    CONVERGENT = "convergent"
    REPORTS = "reports"
    NONE = "none"


class TargetStatus(StrEnum):
    OPEN = "open"
    ESTABLISHED = "established"
    CONTESTED = "contested"
    #: Material needs remain but no viable research action is left for them.
    EXHAUSTED = "exhausted"
    #: Every route to the evidence is unreadable or blocked.
    UNRESOLVABLE = "unresolvable"
    #: Not pursued by this mode.
    DEFERRED = "deferred"


class OriginStatus(StrEnum):
    NOT_SOUGHT = "not_sought"
    SOUGHT_NOT_FOUND = "sought_not_found"
    LOCATED_UNREADABLE = "located_unreadable"
    FOUND = "found"


class TargetNeed(BaseModel):
    kind: NeedKind
    #: Whether resolving it could change the answer or its confidence.
    material: bool
    reason: str = ""
    lead_ids: list[str] = Field(default_factory=list)


class TargetState(BaseModel):
    """What the evidence establishes for one target, and what it still lacks."""

    target_id: str
    tier: Tier = Tier.SUPPORTING
    sufficiency: Sufficiency = Sufficiency.INSUFFICIENT
    basis: Basis = Basis.NONE
    status: TargetStatus = TargetStatus.OPEN
    best_provenance: Provenance | None = None
    has_provenance_a: bool = False
    decisive_evidence_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    independent_groups: int = 0
    origin_status: OriginStatus = OriginStatus.NOT_SOUGHT
    needs: list[TargetNeed] = Field(default_factory=list)
    attempts: dict[str, int] = Field(default_factory=dict)
    #: Steps an intent ran for this target without the fingerprint improving.
    no_gain: dict[str, int] = Field(default_factory=dict)
    fingerprint: list = Field(default_factory=list)
    unchanged_steps: int = 0
    confidence: Confidence = Confidence.LOW
    reasons: list[str] = Field(default_factory=list)

    def material_needs(self) -> list[TargetNeed]:
        return [need for need in self.needs if need.material]


class Action(BaseModel):
    """One research action: what it is for, why that is worth doing, and how it searches."""

    id: str
    target_id: str
    step: int
    intent: Intent
    need: NeedKind
    materiality_reason: str = ""
    subject_ids: list[str] = Field(default_factory=list)
    query: str = ""
    vertical: Literal["web", "news", "scholarly"] = "web"
    time_filter: str | None = None
    lead_id: str | None = None
    #: Which attempt of this intent for this target, so wording can change between them.
    attempt: int = 1


class CompletionStatus(StrEnum):
    """Why a run ended. The same meaning in every mode."""

    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    CEILING_REACHED = "ceiling_reached"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


# --- Claims ----------------------------------------------------------------------------


class Claim(BaseModel):
    id: str
    text: str
    claim_type: ClaimType = ClaimType.FACT
    polarity: Polarity = Polarity.AFFIRMS
    question_id: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] = Field(default_factory=list)
    condition: str | None = None
    support: SupportStatus = SupportStatus.INSUFFICIENT_RESEARCH
    confidence: Confidence = Confidence.LOW
    confidence_reasons: list[str] = Field(default_factory=list)
    verification_notes: list[str] = Field(default_factory=list)
    #: Evidence that disagrees with what this claim's own evidence states, unexplained.
    opposing_evidence_ids: list[str] = Field(default_factory=list)
    #: What the claim's evidence supports before an opposing source is taken into account, so
    #: an audit that finds the opposition is about something else can restore it.
    _support_before_conflict: SupportStatus | None = PrivateAttr(default=None)


class ComparisonDimension(BaseModel):
    dimension_id: str
    name: str
    importance: Importance
    entity_claim_ids: dict[str, list[str]] = Field(default_factory=dict)
    comparison_claim_ids: list[str] = Field(default_factory=list)
    strength_by_entity: dict[str, Strength] = Field(default_factory=dict)


class VerificationDecision(BaseModel):
    claim_id: str
    outcome: Literal["kept", "downgraded", "removed"]
    support: SupportStatus
    reasons: list[str] = Field(default_factory=list)


# --- Trace and outcome -----------------------------------------------------------------


class SearchAttempt(BaseModel):
    #: The research action the search served.
    task_id: str
    query: str
    #: The step it ran in.
    round: int
    #: The action's intent.
    strategy: str
    vertical: str = "web"
    provider: str = ""
    status: Literal["useful", "weak", "empty", "error", "skipped_duplicate", "skipped_budget"]
    result_count: int = 0
    usable: int = 0
    relevant: int = 0
    unique_hosts: int = 0
    novelty: float = 0.0
    error: str | None = None


class FetchRecord(BaseModel):
    source_id: str | None = None
    url: str
    outcome: Literal["fetched", "cached", "failed", "skipped"]
    http_status: int | None = None
    error: str | None = None


class RejectionRecord(BaseModel):
    stage: Literal["search", "fetch", "extraction", "authority", "dedup", "evidence"]
    ref: str
    reason: RejectionReason
    detail: str = ""


class ChunkSelection(BaseModel):
    round: int
    question_id: str
    chunk_id: str
    slot: Literal["priority", "source_diverse", "exploration"]
    priority: float = 0.0


class SourceClassification(BaseModel):
    source_id: str
    source_class: SourceClass
    official_for: list[str] = Field(default_factory=list)
    model_label: SourceClass | None = None
    signals: list[str] = Field(default_factory=list)
    note: str = ""


class BudgetUsage(BaseModel):
    searches: int = 0
    fetches: int = 0
    llm_calls: int = 0
    seconds: float = 0.0
    #: Times the run read on past its mode's usual page allowance, because a core question
    #: was still unanswered and the evidence was still improving.
    read_extensions: int = 0


class StageTiming(BaseModel):
    """What one stage spent with the model, so a run can show where its time went."""

    stage: str
    calls: int = 0
    retries: int = 0
    truncations: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0


class ReportSentence(BaseModel):
    """One line of the report, with what it stands on.

    The chain a reader can follow runs sentence → claim → evidence → the sentences of the page
    it was read from, so nothing in a report is unattributable.
    """

    section: str
    text: str
    claim_ids: list[str] = Field(default_factory=list)
    citation_numbers: list[int] = Field(default_factory=list)
    #: The exact evidence used for this sentence, including audited explanatory detail.
    evidence_ids: list[str] = Field(default_factory=list)


class ResearchTrace(BaseModel):
    run_id: str
    query: str
    depth: DepthMode
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    plan_issues: list[PlanIssue] = Field(default_factory=list)
    search_tasks: list[SearchTask] = Field(default_factory=list)
    search_attempts: list[SearchAttempt] = Field(default_factory=list)
    fetches: list[FetchRecord] = Field(default_factory=list)
    rejections: list[RejectionRecord] = Field(default_factory=list)
    chunk_selections: list[ChunkSelection] = Field(default_factory=list)
    # Chunk text is kept only for chunks that ground accepted evidence; others keep a hash.
    chunks: list[Chunk] = Field(default_factory=list)
    classifications: list[SourceClassification] = Field(default_factory=list)
    leads: list[OriginLead] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    report_sentences: list[ReportSentence] = Field(default_factory=list)
    target_states: list[TargetState] = Field(default_factory=list)
    completion_status: CompletionStatus | None = None
    contradiction_groups: list[ContradictionGroup] = Field(default_factory=list)
    removed_claims: list[Claim] = Field(default_factory=list)
    verification: list[VerificationDecision] = Field(default_factory=list)
    confidence_reasons: list[str] = Field(default_factory=list)
    budget: BudgetUsage = Field(default_factory=BudgetUsage)
    #: The route the run used and what that model can do, never a model Research chose.
    model_route: dict[str, Any] = Field(default_factory=dict)
    stages: list[StageTiming] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    stop_detail: str = ""
    errors: list[str] = Field(default_factory=list)


class ResearchMetrics(BaseModel):
    searches_run: int = 0
    sources_reviewed: int = 0
    sources_used: int = 0
    independent_domains: int = 0
    primary_sources: int = 0
    evidence_accepted: int = 0
    evidence_rejected: int = 0
    open_gaps: int = 0


class ResearchOutcome(BaseModel):
    query: str
    depth: DepthMode
    plan: ResearchPlan | None = None
    sources: list[ResearchSource] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    comparison: list[ComparisonDimension] = Field(default_factory=list)
    confidence: Confidence = Confidence.LOW
    confidence_reasons: list[str] = Field(default_factory=list)
    report: str = ""
    report_sections: list[str] = Field(default_factory=list)
    report_type: Literal["complete", "partial", "failed"] = "failed"
    stop_reason: StopReason = StopReason.FAILED
    completion_status: CompletionStatus = CompletionStatus.FAILED
    stop_detail: str = ""
    target_states: list[TargetState] = Field(default_factory=list)
    metrics: ResearchMetrics = Field(default_factory=ResearchMetrics)
    trace: ResearchTrace


# --- Jobs ------------------------------------------------------------------------------


class JobStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    SEARCHING = "searching"
    FETCHING = "fetching"
    EXTRACTING = "extracting"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressEvent(BaseModel):
    status: str
    progress_percent: int = 0
    current_step: str = ""
    message: str = ""
    searches_run: int = 0
    sources_reviewed: int = 0
    sources_used: int = 0
    evidence_accepted: int = 0
    open_gaps: int = 0
    timestamp: str = ""
    #: What the run is doing, in the reader's terms rather than the engine's stage names.
    phase: str = ""
    #: How the answer stands while the run is still going.
    core_established: int = 0
    core_targets: int = 0
    elapsed_seconds: int = 0
    ceiling_seconds: int = 0


class ResearchJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    user_query: str
    depth: DepthMode = DepthMode.STANDARD
    max_sources: int = 16
    max_rounds: int = 2
    status: JobStatus = JobStatus.QUEUED
    created_at: str = ""
    updated_at: str = ""
    progress_percent: int = 0
    current_step: str = ""
    plan: ResearchPlan | None = None
    generated_queries: list[str] = Field(default_factory=list)
    sources: list[ResearchSource] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    trace: ResearchTrace | None = None
    report: str = ""
    error: str | None = None
    owner_id: str | None = None
    database_identity: str | None = None
    profile_id: str | None = None
    is_guest: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    progress_log: list[ProgressEvent] = Field(default_factory=list)
    # Rows written before structured evidence existed. Kept readable, never used as input.
    legacy: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _isolate_legacy_shapes(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        data = dict(data)
        legacy: dict[str, Any] = dict(data.get("legacy") or {})
        if "evidence_chunks" in data:
            legacy["evidence_chunks"] = data.pop("evidence_chunks")
        evidence = data.get("evidence") or []
        if any(isinstance(item, dict) and "excerpt" not in item for item in evidence):
            legacy["evidence_chunks"] = data.pop("evidence")
        plan = data.get("plan")
        if isinstance(plan, dict) and "understanding" not in plan:
            legacy["plan"] = data.pop("plan")
        sources = data.get("sources") or []
        if any(isinstance(item, dict) and "host_group" not in item for item in sources):
            legacy["sources"] = data.pop("sources")
        log = data.get("progress_log") or []
        data["progress_log"] = [
            {key: value for key, value in item.items() if key in ProgressEvent.model_fields}
            for item in log
            if isinstance(item, dict)
        ]
        if legacy:
            data["legacy"] = legacy
        return data

    def current_progress(self) -> ProgressEvent:
        open_gaps = 0
        if self.trace and self.trace.target_states:
            open_gaps = sum(
                1
                for state in self.trace.target_states
                if state.sufficiency != Sufficiency.ESTABLISHED and state.tier == Tier.CORE
            )
        return ProgressEvent(
            status=self.status.value,
            progress_percent=self.progress_percent,
            current_step=self.current_step,
            message=self.progress_log[-1].message if self.progress_log else "",
            searches_run=len(self.generated_queries),
            sources_reviewed=sum(1 for s in self.sources if s.reviewed),
            sources_used=sum(1 for s in self.sources if s.used),
            evidence_accepted=sum(1 for e in self.evidence if e.status == "accepted"),
            open_gaps=open_gaps,
            timestamp=datetime.now(UTC).isoformat(),
        )


class StartResearchRequest(BaseModel):
    query: str
    depth: DepthMode = DepthMode.STANDARD
    project_id: str | None = None
    task_id: str | None = None
    repo_id: str | None = None
    memory_enabled: bool = True
    incognito: bool = False
