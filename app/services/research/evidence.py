"""The evidence gate: candidate selection, model judgement, deterministic acceptance.

Candidate selection optimises precision without silently destroying recall: lexical rank is
one signal among several, and a share of every question's slots goes to source diversity
and to exploration outside the lexical top set. The model then judges each candidate
against the question. Acceptance is categorical and decided by code: the excerpt must be
verbatim text of the retrieved chunk, every reference must exist, and a chunk that merely
mentions an entity is not evidence.
"""

from __future__ import annotations

import math
import random
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import zip_longest

from pydantic import BaseModel, ConfigDict, Field

from app.services.research.capability import items_per_call
from app.services.research.llm_json import (
    LLMBudget,
    LLMBudgetExhausted,
    LLMFactory,
    ModelOutputInvalid,
    call_json,
    truncations,
)
from app.services.research.textutil import (
    compact,
    grounding_form,
    is_grounded,
    is_propositional,
    jaccard,
    mentions_any,
    name_forms,
    names_entity,
    normalize_ws,
    parse_date,
    split_sentences,
    token_set,
    tokens,
)
from app.services.research.types import (
    AuthorityKind,
    Chunk,
    ChunkSelection,
    ClaimFit,
    ClaimScope,
    ContradictionGroup,
    EvidenceItem,
    EvidenceTarget,
    FreshnessFit,
    LeadKind,
    OriginLead,
    Polarity,
    Provenance,
    RecencyKind,
    RecordForm,
    RejectionReason,
    RejectionRecord,
    Relevance,
    ResearchPlan,
    ResearchSource,
    ResearchTrace,
    Support,
    TargetForm,
)

_PROVENANCE_SCORE = {
    Provenance.A: 1.0,
    Provenance.B: 0.7,
    Provenance.C: 0.4,
    Provenance.D: 0.0,
}
NUMERIC_CONFLICT_TOLERANCE = 0.15
# Chunk text per judge call. Keeps prompts well inside modest context windows.
JUDGE_BATCH_CHARS = 12_000
#: Output one judged chunk costs: a finding or two, each carrying a verbatim excerpt.
JUDGE_OUTPUT_TOKENS_PER_CHUNK = 220

JUDGE_INSTRUCTIONS = """\
Judge whether each text chunk contains evidence for the listed research questions.

Each chunk's text is given as numbered sentences. For every chunk return zero or more findings.
A finding is one fact the chunk itself states that helps answer one listed question:
- question_id: the listed question the fact answers.
- entity_ids: the research entities the fact is actually about (may be empty).
- relevance: high, medium, low or none, relative to that question.
- support: direct when the sentences state the answer for that question and those entities;
  partial when they only cover part of it or imply it; none otherwise.
- sentence_ids: the numbers of the sentences that state the fact, and only those.
- claim: the fact restated neutrally in at most 25 words, adding nothing.
- polarity: affirms, or negates only when the text explicitly says something is not the case.
  Never infer a negative from something the text does not mention.
- attribute: a short snake_case name for the specific property the fact states (the particular
  outcome, measure or behaviour), without entity names, never the question's overall topic.
  Reuse a known_attributes name only when it names exactly the property this fact states.
- value, unit, as_of: only when the text gives them.
- scope: the place, population, version, tier or condition the text limits the fact to, when
  it states one. Never an entity name or a question label.
- evidence_scope: record (what a specific document, issuer or study states), instance (one
  event, case or person's report) or general (a generalisation about a population, product
  or trend).
- single_report: true when the fact is one person's or one case's own account.
- method_stated: true when the sentences say how a general finding was reached (a sample,
  test, survey or source data).
- lead: when the chunk cites or links the original record the fact comes from, give
  {"title", "issuer", "year"} exactly as the chunk states them; otherwise omit it.
- contradicts_evidence_ids: ids from existing_evidence that this fact materially disagrees with.

For every listed source also return what kind of publisher it is:
- sources: [{"source_id", "role", "issuer", "review_status"}] role is issuer_record (the
  publisher's own record, statement, data, product documentation or study), institutional (a
  public or scholarly institution publishing in its own capacity), independent_expert,
  reporting, reference, community, aggregator (copies, mirrors or lists others' content),
  promotional or unknown. issuer is who published the page, as the page itself says.
  review_status is peer_reviewed, preprint or official when the page says so, else empty.

A chunk that only mentions an entity, or is about a different subject, has no findings.
A fact answers a question only about what it is itself about: when its sentences do not say
what that is, the chunk must make it unmistakable, and a fact about something the question
does not ask about (another product, treatment, group or event) is no finding for it.
Keyword lists, tags, navigation, bylines, captions, titles and label: value metadata name
things without stating a fact about them, and are never findings.
A generic statement that names no concrete property, value, condition, mechanism or
comparison relevant to the question is not a finding.
Output, with sources first: {"sources": [...], "chunks": [{"chunk_id": "...", "findings": [...]}]}
"""


class _Lead(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    title: str = ""
    issuer: str = ""
    year: str = ""


class _SourceRoleLabel(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    source_id: str = ""
    role: str = "unknown"
    issuer: str = ""
    review_status: str = ""


class _Finding(BaseModel):
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)

    question_id: str = ""
    entity_ids: list[str] = Field(default_factory=list)
    relevance: str = "none"
    support: str = "none"
    claim: str = ""
    excerpt: str = ""
    polarity: str = "affirms"
    subject: str | None = None
    attribute: str | None = None
    value: str | None = None
    unit: str | None = None
    scope: str | None = None
    qualifiers: list[str] = Field(default_factory=list)
    as_of: str | None = None
    contradicts_evidence_ids: list[str] = Field(default_factory=list)
    sentence_ids: list[int] = Field(default_factory=list)
    evidence_scope: str | None = None
    single_report: bool = False
    method_stated: bool = False
    lead: _Lead | None = None


class _JudgedChunk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunk_id: str = ""
    findings: list[_Finding] = Field(default_factory=list)


class _JudgeOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chunks: list[_JudgedChunk] = Field(default_factory=list)
    sources: list[_SourceRoleLabel] = Field(default_factory=list)


@dataclass
class JudgeResult:
    """What one round of judging produced."""

    items: list[EvidenceItem]
    #: Pairs of evidence ids the judge said materially disagree.
    conflict_links: list[tuple[str, str]]
    #: The judge's reading of each source it saw: role, issuer and review status.
    roles: dict[str, _SourceRoleLabel]
    #: Originating records the judged chunks cite, grounded in their text.
    leads: list[OriginLead]


@dataclass
class Selection:
    chunk: Chunk
    slot: str
    priority: float


class EvidencePool:
    """Chunks eligible for judgement, and which (question, chunk) pairs were already judged."""

    def __init__(self) -> None:
        self.chunks: dict[str, Chunk] = {}
        self.sources: dict[str, ResearchSource] = {}
        self.judged: set[tuple[str, str]] = set()

    def add(self, source: ResearchSource, chunks: list[Chunk]) -> None:
        self.sources[source.id] = source
        for chunk in chunks:
            self.chunks[chunk.id] = chunk

    def eligible(self) -> list[Chunk]:
        return [
            chunk
            for chunk in self.chunks.values()
            if (source := self.sources.get(chunk.source_id)) is not None
            and source.rejection_reason is None
            and source.duplicate_of is None
        ]


def select_candidates(
    plan: ResearchPlan,
    target: EvidenceTarget,
    pool: EvidencePool,
    k: int,
    seed: str,
    semantic_scores: dict[str, float] | None = None,
    *,
    only_sources: set[str] | None = None,
    per_source: int = 2,
) -> list[Selection]:
    """The chunks most worth judging for one target.

    Lexical fit is one signal among several, and a share of the slots goes to chunks from
    other sources and to exploration, so a relevant passage in different words still reaches
    the judge. ``only_sources`` limits a target that is already established to the pages read
    for its remaining need, so settled targets are not judged again on everything read.
    """
    eligible = [
        chunk
        for chunk in pool.eligible()
        if (target.id, chunk.id) not in pool.judged
        and (only_sources is None or chunk.source_id in only_sources)
    ]
    if not eligible or k <= 0:
        return []
    dimension = plan.dimension(target.dimension_id)
    constraint_text = " ".join(
        c.text for c in plan.understanding.constraints if c.id in target.constraint_ids
    )
    query_tokens = tokens(
        f"{target.text} {target.scope_note} {dimension.name if dimension else ''} "
        f"{dimension.description if dimension else ''} {constraint_text}"
    )
    forms: set[str] = set()
    for subject_id in target.subject_ids:
        entity = plan.entity(subject_id)
        if entity:
            forms |= name_forms(entity.names())
    lexical = _bm25(query_tokens, eligible)
    top_lexical = max(lexical.values(), default=0.0) or 1.0
    signature = token_set(f"{target.text} {dimension.name if dimension else ''}")

    priorities: dict[str, float] = {}
    standing: dict[str, float] = {}
    alias_hits: dict[str, bool] = {}
    for chunk in eligible:
        source = pool.sources[chunk.source_id]
        heading_text = " ".join(chunk.heading_path)
        alias_hit = bool(forms) and mentions_any(f"{heading_text} {chunk.text}", forms)
        alias_hits[chunk.id] = alias_hit
        heading = min(1.0, 3 * jaccard(token_set(heading_text), signature))
        grade = source.assessment.provenance if source.assessment else None
        standing[chunk.id] = _PROVENANCE_SCORE.get(grade, 0.5)
        priority = (
            0.45 * (lexical.get(chunk.id, 0.0) / top_lexical)
            + 0.2 * (1.0 if alias_hit else (0.0 if forms else 0.5))
            + 0.15 * heading
            + 0.2 * standing[chunk.id]
            + 0.3 * (semantic_scores or {}).get(chunk.id, 0.0)
        )
        priorities[chunk.id] = round(priority, 4)

    ordered = sorted(eligible, key=lambda chunk: (-priorities[chunk.id], chunk.id))
    k_priority = max(1, round(k * 0.6))
    k_diverse = round(k * 0.25)
    k_explore = max(0, k - k_priority - k_diverse)
    per_source_count: dict[str, int] = defaultdict(int)

    def room(chunk: Chunk) -> bool:
        return per_source_count[chunk.source_id] < per_source

    chosen: list[Selection] = []
    for chunk in ordered:
        if len(chosen) >= k_priority:
            break
        if room(chunk):
            chosen.append(Selection(chunk, "priority", priorities[chunk.id]))
            per_source_count[chunk.source_id] += 1
    taken = {selection.chunk.id for selection in chosen}
    represented = {selection.chunk.source_id for selection in chosen}
    for chunk in ordered:
        if len(chosen) >= k_priority + k_diverse:
            break
        if chunk.id in taken or chunk.source_id in represented:
            continue
        chosen.append(Selection(chunk, "source_diverse", priorities[chunk.id]))
        per_source_count[chunk.source_id] += 1
        taken.add(chunk.id)
        represented.add(chunk.source_id)

    exploration = [
        chunk
        for chunk in sorted(eligible, key=lambda chunk: chunk.id)
        if chunk.id not in taken and standing[chunk.id] > 0 and room(chunk)
    ]
    random.Random(f"{seed}:{target.id}").shuffle(exploration)
    exploration.sort(key=lambda chunk: not alias_hits[chunk.id])
    for chunk in exploration[:k_explore]:
        chosen.append(Selection(chunk, "exploration", priorities[chunk.id]))
    return chosen


def judge_candidates(
    plan: ResearchPlan,
    selections: dict[str, list[Selection]],
    pool: EvidencePool,
    existing: list[EvidenceItem],
    *,
    llm_factory: LLMFactory,
    budget: LLMBudget,
    trace: ResearchTrace,
    round_no: int,
    today: date,
    out_of_time: Callable[[], bool] | None = None,
    reserved: bool = False,
    known_leads: list[OriginLead] | None = None,
    batch_size: int = 10,
    max_calls: int | None = None,
    share_existing: bool = True,
) -> JudgeResult:
    """Judge selected chunks: evidence items, conflict links, source roles and cited records.

    Judging is where a round spends most of its time, so the deadline is checked per batch:
    a depth's time budget bounds the research it does, not only the rounds it counts.
    """
    questions_by_chunk: dict[str, list[str]] = defaultdict(list)
    for question_id, chosen in selections.items():
        for selection in chosen:
            if question_id not in questions_by_chunk[selection.chunk.id]:
                questions_by_chunk[selection.chunk.id].append(question_id)
            trace.chunk_selections.append(
                ChunkSelection(
                    round=round_no,
                    question_id=question_id,
                    chunk_id=selection.chunk.id,
                    slot=selection.slot,  # type: ignore[arg-type]
                    priority=selection.priority,
                )
            )
    chunk_ids = _interleaved(selections)
    new_items: list[EvidenceItem] = []
    conflict_links: list[tuple[str, str]] = []
    roles: dict[str, _SourceRoleLabel] = {}
    leads: list[OriginLead] = []
    lead_keys = {_lead_key(lead) for lead in known_leads or []}
    lead_counter = len(known_leads or [])
    counter = len(existing)
    accepted_by_excerpt = {
        (item.question_id, grounding_form(item.excerpt).lower()): item.id
        for item in existing
        if item.status == "accepted"
    }

    pending = judge_batches(
        chunk_ids,
        pool,
        batch_size,
        budget,
        {chunk_id: len(questions) for chunk_id, questions in questions_by_chunk.items()},
    )
    calls = 0
    while pending:
        batch_ids = pending.pop(0)
        batch_questions = {qid for chunk_id in batch_ids for qid in questions_by_chunk[chunk_id]}
        spent = "time limit reached" if out_of_time and out_of_time() else None
        if spent is None and not budget.can_spend(reserved=reserved):
            spent = "model budget exhausted"
        if spent is None and max_calls is not None and calls >= max_calls:
            spent = "this step's judging limit reached"
        if spent is not None:
            for chunk_id in batch_ids:
                for question_id in questions_by_chunk[chunk_id]:
                    trace.rejections.append(
                        RejectionRecord(
                            stage="evidence",
                            ref=f"{question_id}:{chunk_id}",
                            reason=RejectionReason.BUDGET,
                            detail=f"not judged: {spent}",
                        )
                    )
            continue
        batch_sources = list(
            dict.fromkeys(pool.chunks[chunk_id].source_id for chunk_id in batch_ids)
        )
        # What the batch is judged against comes first and changes least between calls, so a
        # local runtime can reuse its prompt cache; the chunks come last.
        payload: dict = {
            "entities": [
                {"id": e.id, "name": e.name, "type": e.type, "aliases": e.aliases}
                for e in plan.understanding.entities
            ],
            # Only the questions this batch was picked for: every listed question costs
            # output, and a model with little room to answer spends it on the wrong ones.
            "questions": [
                _question_payload(plan, question.id)
                for question in plan.targets
                if question.id in batch_questions
            ],
            "known_attributes": _attribute_registry(existing, batch_questions),
            "sources": [_source_payload(pool.sources[source_id]) for source_id in batch_sources],
        }
        if share_existing:
            payload["existing_evidence"] = [
                {
                    "id": item.id,
                    "question_id": item.question_id,
                    "entity_ids": item.entity_ids,
                    "attribute": item.attribute,
                    "value": item.value,
                    "claim": item.claim[:160],
                }
                for item in existing
                if item.status == "accepted" and item.question_id in batch_questions
            ][:25]
        payload["chunks"] = [
            _chunk_payload(pool, chunk_id, questions_by_chunk[chunk_id]) for chunk_id in batch_ids
        ]
        cut_off_before = truncations(budget, "evidence_judgement")
        calls += 1
        try:
            output = call_json(
                llm_factory,
                stage="evidence_judgement",
                instructions=JUDGE_INSTRUCTIONS,
                payload=payload,
                schema=_JudgeOutput,
                budget=budget,
                num_predict=4500,
                notes=trace.errors,
                reserved=reserved,
            )
        except (ModelOutputInvalid, LLMBudgetExhausted) as exc:
            trace.errors.append(f"evidence judgement batch skipped: {exc}")
            for chunk_id in batch_ids:
                for question_id in questions_by_chunk[chunk_id]:
                    pool.judged.add((question_id, chunk_id))
                    trace.rejections.append(
                        RejectionRecord(
                            stage="evidence",
                            ref=f"{question_id}:{chunk_id}",
                            reason=RejectionReason.INVALID_REFERENCE,
                            detail="judge output unusable",
                        )
                    )
            continue

        for label in output.sources:
            if label.source_id in batch_sources:
                roles[label.source_id] = label
        with_findings: set[tuple[str, str]] = set()
        budget.profile.usage("evidence_judgement").items += len(
            {judged.chunk_id for judged in output.chunks} & set(batch_ids)
        )
        for judged in output.chunks:
            chunk = pool.chunks.get(judged.chunk_id)
            if chunk is None or judged.chunk_id not in questions_by_chunk:
                trace.rejections.append(
                    RejectionRecord(
                        stage="evidence",
                        ref=judged.chunk_id,
                        reason=RejectionReason.INVALID_REFERENCE,
                        detail="judge referenced an unknown chunk",
                    )
                )
                continue
            for finding in judged.findings:
                counter += 1
                item, links = _accept(
                    plan,
                    finding,
                    chunk,
                    pool,
                    item_id=f"ev{counter}",
                    round_no=round_no,
                    today=today,
                    accepted_by_excerpt=accepted_by_excerpt,
                    existing_ids={e.id for e in existing} | {e.id for e in new_items},
                )
                new_items.append(item)
                conflict_links.extend(links)
                if item.status == "accepted" and finding.lead is not None:
                    lead = _grounded_lead(
                        finding.lead, item, chunk, pool.sources[chunk.source_id], lead_counter + 1
                    )
                    if lead is not None and _lead_key(lead) not in lead_keys:
                        lead_counter += 1
                        lead_keys.add(_lead_key(lead))
                        leads.append(lead)
                        item.lead_ids.append(lead.id)
                if item.status == "accepted":
                    with_findings.add((item.question_id, chunk.id))
                    pool.judged.add((item.question_id, chunk.id))
                    if item.duplicate_of is None:
                        accepted_by_excerpt[
                            (item.question_id, grounding_form(item.excerpt).lower())
                        ] = item.id
                else:
                    trace.rejections.append(
                        RejectionRecord(
                            stage="evidence",
                            ref=item.id,
                            reason=item.rejection_reason or RejectionReason.IRRELEVANT,
                            detail=item.claim[:160],
                        )
                    )
        answered = {judged.chunk_id for judged in output.chunks}
        unanswered = [chunk_id for chunk_id in batch_ids if chunk_id not in answered]
        if (
            truncations(budget, "evidence_judgement") > cut_off_before
            and unanswered
            and len(batch_ids) > 1
            and budget.can_spend()
        ):
            # The answer ran out of room before these chunks. Judge them in smaller
            # batches rather than recording them as having nothing to say.
            half = max(1, len(unanswered) // 2)
            pending[:0] = [unanswered[:half], unanswered[half:]]
            trace.errors.append(
                f"evidence judgement split into smaller batches after a cut-off answer: "
                f"{len(unanswered)} chunk(s) re-judged"
            )
            continue
        for chunk_id in batch_ids:
            for question_id in questions_by_chunk[chunk_id]:
                pool.judged.add((question_id, chunk_id))
                if (question_id, chunk_id) not in with_findings:
                    trace.rejections.append(
                        RejectionRecord(
                            stage="evidence",
                            ref=f"{question_id}:{chunk_id}",
                            reason=RejectionReason.IRRELEVANT,
                            detail="no accepted finding",
                        )
                    )
    return JudgeResult(new_items, conflict_links, roles, leads)


def _judged_chunk_cost(budget: LLMBudget) -> int:
    """What one judged chunk costs this model, learnt from how often it has been cut off.

    The estimate below is a starting point. A model that keeps running out of room is
    telling us its findings are longer than assumed, so each cut-off answer raises the
    assumed cost and shrinks the next batch. Believing it costs a little breadth per call
    and saves judging the same chunks twice.
    """
    usage = budget.profile.usage("evidence_judgement")
    measured = usage.completion_tokens_per_item()
    if measured is not None:
        # What this model actually writes per chunk, with room for a longer answer than usual.
        return max(60, math.ceil(measured * 1.3))
    return JUDGE_OUTPUT_TOKENS_PER_CHUNK * (1 + min(3, usage.truncations))


def _interleaved(selections: dict[str, list[Selection]]) -> list[str]:
    """Chunk ids ordered so every question's best chunk is judged before any question's second.

    A call may not reach the end of its batch, and the questions that come later in a plan are
    no less part of the answer, so the order rotates between them rather than working through
    one question at a time.
    """
    queues = [
        list(dict.fromkeys(selection.chunk.id for selection in chosen))
        for chosen in selections.values()
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for row in zip_longest(*queues):
        for chunk_id in row:
            if chunk_id is not None and chunk_id not in seen:
                seen.add(chunk_id)
                ordered.append(chunk_id)
    return ordered


def judge_batches(
    chunk_ids: list[str],
    pool: EvidencePool,
    size: int,
    budget: LLMBudget | None = None,
    questions_per_chunk: dict[str, int] | None = None,
) -> list[list[str]]:
    """Group chunks into calls the routed model can actually answer in full.

    The depth asks for a batch size; the model decides what fits. A model with a small
    output limit gets fewer chunks per call rather than an answer cut off halfway.

    What a chunk costs to judge is a finding per question it was picked for, so a chunk that
    answers three questions is charged as three: counting chunks alone is what produced answers
    cut off at the model's limit, and a cut-off answer costs a whole call for part of a batch.
    """
    weight = questions_per_chunk or {}

    def cost(chunk_id: str) -> int:
        return max(1, weight.get(chunk_id, 1))

    if budget is not None:
        average = max(1, sum(len(pool.chunks[c].text) for c in chunk_ids) // max(1, len(chunk_ids)))
        size = min(
            size,
            items_per_call(
                budget.capability,
                output_tokens_per_item=_judged_chunk_cost(budget),
                overhead_tokens=120,
                input_chars_per_item=average,
                ceiling=size,
            ),
        )
    batches: list[list[str]] = []
    current: list[str] = []
    chars = 0
    findings = 0
    for chunk_id in chunk_ids:
        length = len(pool.chunks[chunk_id].text)
        over_budget = findings and findings + cost(chunk_id) > size
        if current and (over_budget or chars + length > JUDGE_BATCH_CHARS):
            batches.append(current)
            current, chars, findings = [], 0, 0
        current.append(chunk_id)
        chars += length
        findings += cost(chunk_id)
    if current:
        batches.append(current)
    return batches


def _accept(
    plan: ResearchPlan,
    finding: _Finding,
    chunk: Chunk,
    pool: EvidencePool,
    *,
    item_id: str,
    round_no: int,
    today: date,
    accepted_by_excerpt: dict[tuple[str, str], str],
    existing_ids: set[str],
) -> tuple[EvidenceItem, list[tuple[str, str]]]:
    source = pool.sources[chunk.source_id]
    question = plan.target(finding.question_id)
    entity_lookup = {e.id: e.id for e in plan.understanding.entities}
    for entity in plan.understanding.entities:
        for name in entity.names():
            entity_lookup.setdefault(compact(name), entity.id)
    resolved = [
        entity_lookup.get(raw) or entity_lookup.get(compact(raw)) for raw in finding.entity_ids
    ]
    entity_ids = list(dict.fromkeys(entity_id for entity_id in resolved if entity_id))
    relevance = _enum(Relevance, finding.relevance, Relevance.NONE)
    support = _enum(Support, finding.support, Support.NONE)
    polarity = _enum(Polarity, finding.polarity, Polarity.AFFIRMS)
    claim = normalize_ws(finding.claim)[:400]
    cited = _cited_sentences(chunk, finding.sentence_ids)
    # Sentence numbers make the excerpt the chunk's own words; a model that quoted text
    # instead is held to the verbatim check.
    quoted = " ".join(cited) if cited else finding.excerpt
    excerpt = grounding_form(quoted)[:600]

    reason: RejectionReason | None = None
    if question is None:
        reason = RejectionReason.INVALID_REFERENCE
    elif len(entity_ids) < len(finding.entity_ids):
        reason = RejectionReason.INVALID_REFERENCE
    elif not claim:
        reason = RejectionReason.NO_SUPPORT
    elif relevance in (Relevance.LOW, Relevance.NONE):
        reason = RejectionReason.IRRELEVANT
    elif support == Support.NONE:
        reason = RejectionReason.NO_SUPPORT
    elif finding.sentence_ids and not cited:
        reason = RejectionReason.INVALID_REFERENCE
    elif not is_grounded(quoted, chunk.text):
        reason = RejectionReason.UNGROUNDED_EXCERPT
    elif not is_propositional(excerpt):
        reason = RejectionReason.NON_PROPOSITIONAL
    elif source.rejection_reason is not None:
        reason = source.rejection_reason
    elif question.subject_ids and entity_ids and not set(entity_ids) <= set(question.subject_ids):
        reason = RejectionReason.WRONG_ENTITY

    attribution = "stated"
    if reason is None:
        reason, attribution = _subject_attribution(plan, question, quoted, chunk)
    if reason is None and entity_ids:
        context_text = f"{' '.join(chunk.heading_path)} {chunk.text}"
        named = all(
            mentions_any(context_text, name_forms(entity.names()))
            for entity_id in entity_ids
            if (entity := plan.entity(entity_id)) is not None
        )
        if not named:
            attribution = "inferred"
    if reason is None and attribution == "inferred" and support == Support.DIRECT:
        support = Support.PARTIAL

    item = EvidenceItem(
        id=item_id,
        chunk_id=chunk.id,
        source_id=source.id,
        question_id=finding.question_id if question else finding.question_id[:40],
        entity_ids=entity_ids,
        claim=claim,
        excerpt=excerpt,
        polarity=polarity,
        attribution=attribution,  # type: ignore[arg-type]
        subject=_short(finding.subject),
        attribute=_short(finding.attribute),
        value=_short(finding.value),
        unit=_short(finding.unit),
        scope=_stated_scope(plan, finding.scope),
        qualifiers=[normalize_ws(q)[:80] for q in finding.qualifiers[:5] if normalize_ws(q)],
        as_of=_short(finding.as_of),
        relevance=relevance,
        support=support,
        source_class=source.source_class,
        extraction_quality=source.extraction_quality,
        freshness_fit=freshness_fit(
            question, source.modified_date or source.published_date, finding.as_of, today
        ),
        host_group=source.host_group,
        status="rejected" if reason else "accepted",
        rejection_reason=reason,
        round=round_no,
        sentence_ids=[number for number in finding.sentence_ids if cited] if cited else [],
        evidence_scope=_enum(ClaimScope, finding.evidence_scope, None),
        single_report=bool(finding.single_report),
        method_stated=bool(finding.method_stated),
    )
    if reason is None:
        duplicate = accepted_by_excerpt.get((item.question_id, excerpt.lower()))
        if duplicate is not None:
            item.duplicate_of = duplicate
    links = [
        (item.id, other)
        for other in finding.contradicts_evidence_ids
        if reason is None and other in existing_ids
    ]
    return item, links


def _asked_entities(plan: ResearchPlan, target: EvidenceTarget) -> set[str]:
    """The entities a target's question asks about, including those of the questions above it."""
    asked: set[str] = set()
    current: EvidenceTarget | None = target
    while current is not None:
        asked.update(current.subject_ids)
        current = plan.target(current.parent_id) if current.parent_id else None
    return asked


def _named_entities(plan: ResearchPlan, text: str) -> tuple[set[str], set[str]]:
    """The entities ``text`` names, and those code cannot tell about (an acronym, say)."""
    named: set[str] = set()
    unclear: set[str] = set()
    for entity in plan.understanding.entities:
        others = [
            name
            for other in plan.understanding.entities
            if other.id != entity.id
            for name in other.names()
        ]
        verdict = names_entity(text, entity.names(), others)
        if verdict is True or mentions_any(text, name_forms(entity.names())):
            named.add(entity.id)
        elif verdict is None:
            unclear.add(entity.id)
    return named, unclear


def _subject_attribution(
    plan: ResearchPlan, target: EvidenceTarget, excerpt: str, chunk: Chunk
) -> tuple[RejectionReason | None, str]:
    """Whether a fact is about what its question asks, and whether its own words say so.

    A fact is evidence for a question when its excerpt names what the question asks about or,
    when the excerpt names nothing, when nothing else in its section competes: the heading,
    or else the passage, names no other entity of the research. A heading says what its section
    is about, so an incidental mention further down the passage does not override it. A
    passage naming another entity of the research leaves the fact's subject undecided, and the
    finding is not filed under a question on a guess, however well the rest of it fits.
    """
    asked = _asked_entities(plan, target)
    if not asked:
        return None, "stated"
    in_excerpt, unclear_excerpt = _named_entities(plan, excerpt)
    if in_excerpt & asked:
        return None, "stated"
    if in_excerpt - asked:
        return RejectionReason.WRONG_ENTITY, "stated"
    if unclear_excerpt & asked:
        return None, "inferred"
    heading, _ = _named_entities(plan, " ".join(chunk.heading_path))
    if heading & asked and not heading - asked:
        return None, "inferred"
    if heading - asked and not heading & asked:
        return RejectionReason.WRONG_ENTITY, "inferred"
    passage, _ = _named_entities(plan, f"{' '.join(chunk.heading_path)} {chunk.text}")
    if passage - asked:
        return RejectionReason.WRONG_ENTITY, "inferred"
    # With no other subject in sight there is nothing to confuse the fact with, whether or not
    # the passage repeats the subject's name: it stands as inferred, which the audit checks.
    return None, "inferred"


def _stated_scope(plan: ResearchPlan, value: str | None) -> str | None:
    """The scope a finding states, unless it only echoes an entity or a question label."""
    scope = _short(value)
    if not scope:
        return None
    echoes = {
        compact(member.value)
        for enum in (ClaimScope, TargetForm, AuthorityKind, RecordForm)
        for member in enum
    }
    for entity in plan.understanding.entities:
        echoes.add(compact(entity.id))
        echoes.update(compact(name) for name in entity.names())
    return None if compact(scope) in echoes else scope


def freshness_fit(
    target: EvidenceTarget | None, published: str | None, as_of: str | None, today: date
) -> FreshnessFit:
    """How current evidence is, judged by its own target's recency: old evidence about a settled
    or historical matter is not stale."""
    stated = parse_date(as_of) or parse_date(published)
    if stated is None:
        return FreshnessFit.UNKNOWN
    recency = target.profile.recency if target is not None else None
    if recency is None or recency.kind != RecencyKind.CURRENT or not recency.max_age_days:
        return FreshnessFit.FRESH
    max_age = recency.max_age_days
    age = (today - stated).days
    if age <= max_age:
        return FreshnessFit.FRESH
    if age <= 2 * max_age:
        return FreshnessFit.AGING
    return FreshnessFit.STALE


_FIT_RANK = {
    ClaimFit.ESTABLISHES: 0,
    ClaimFit.SUPPORTS: 1,
    ClaimFit.CONTEXT: 2,
    ClaimFit.INSUFFICIENT: 3,
    None: 4,
}


def resolve_restatements(evidence: list[EvidenceItem], sources: list[ResearchSource]) -> None:
    """Among accepted items quoting the same words for the same target, keep the best-sourced one.

    The same sentence often appears on an original and on its copies, and whichever page was read
    first was judged first. Once sources are graded, the item from the original, with the best
    fit to its target, stands for all of them, whatever order the pages were read in.
    """
    by_source = {source.id: source for source in sources}
    groups: dict[tuple[str, str], list[tuple[int, EvidenceItem]]] = defaultdict(list)
    for index, item in enumerate(evidence):
        if item.status == "accepted":
            groups[(item.question_id, grounding_form(item.excerpt).lower())].append((index, item))

    def rank(entry: tuple[int, EvidenceItem]) -> tuple:
        index, item = entry
        source = by_source.get(item.source_id)
        assessment = source.assessment if source is not None else None
        return (
            source is None or source.duplicate_of is not None,
            _FIT_RANK.get(item.claim_fit, 4),
            _PROVENANCE_SCORE_ORDER.get(assessment.provenance if assessment else None, 4),
            index,
        )

    for members in groups.values():
        keep = min(members, key=rank)[1]
        keep.duplicate_of = None
        for _, item in members:
            if item is not keep:
                item.duplicate_of = keep.id


_PROVENANCE_SCORE_ORDER = {Provenance.A: 0, Provenance.B: 1, Provenance.C: 2, Provenance.D: 3}


def detect_contradictions(
    evidence: list[EvidenceItem],
    groups: list[ContradictionGroup],
    model_links: list[tuple[str, str]],
) -> list[ContradictionGroup]:
    """Group materially conflicting accepted evidence. Returns the updated group list."""
    accepted = {
        item.id: item
        for item in evidence
        if item.status == "accepted" and item.duplicate_of is None
    }
    pairs: list[tuple[str, str, str, str]] = []
    by_key: dict[tuple[str, frozenset[str]], list[EvidenceItem]] = defaultdict(list)
    for item in accepted.values():
        by_key[(item.question_id, frozenset(item.entity_ids))].append(item)
    for items in by_key.values():
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                detail = _conflict(left, right)
                if detail:
                    pairs.append((left.id, right.id, detail, "deterministic"))
    for left_id, right_id in model_links:
        if left_id in accepted and right_id in accepted:
            pairs.append((left_id, right_id, "model flagged a material disagreement", "model"))

    membership = {evidence_id: group for group in groups for evidence_id in group.evidence_ids}
    for left_id, right_id, detail, detected_by in pairs:
        left_group, right_group = membership.get(left_id), membership.get(right_id)
        if left_group and right_group and left_group is right_group:
            continue
        if left_group or right_group:
            group = left_group or right_group
            if left_group and right_group:
                group.evidence_ids = list(
                    dict.fromkeys([*left_group.evidence_ids, *right_group.evidence_ids])
                )
                group.pairs = list(dict.fromkeys([*left_group.pairs, *right_group.pairs]))
                for evidence_id in right_group.evidence_ids:
                    membership[evidence_id] = group
                groups.remove(right_group)
        else:
            left = accepted[left_id]
            group = ContradictionGroup(
                id=f"g{len(groups) + 1}",
                question_id=left.question_id,
                entity_ids=list(left.entity_ids),
                detail=detail,
                detected_by=detected_by,  # type: ignore[arg-type]
            )
            groups.append(group)
        for evidence_id in (left_id, right_id):
            if evidence_id not in group.evidence_ids:
                group.evidence_ids.append(evidence_id)
            membership[evidence_id] = group
        pair = (left_id, right_id)
        if pair not in group.pairs and pair[::-1] not in group.pairs:
            group.pairs.append(pair)
    for group in groups:
        for evidence_id in group.evidence_ids:
            if evidence_id in accepted:
                accepted[evidence_id].contradiction_group = group.id
    return groups


def _numbers(value: str | None) -> list[float]:
    return [
        float(item.replace(",", "")) for item in re.findall(r"-?\d[\d,]*(?:\.\d+)?", value or "")
    ]


def _conflict(left: EvidenceItem, right: EvidenceItem) -> str | None:
    if left.source_id == right.source_id or not left.attribute or not right.attribute:
        return None
    left_attr = set(left.attribute.lower().replace("_", " ").split())
    right_attr = set(right.attribute.lower().replace("_", " ").split())
    if jaccard(left_attr, right_attr) < 0.6:
        return None
    if left.scope and right.scope and compact(left.scope) != compact(right.scope):
        return None
    if left.as_of and right.as_of and parse_date(left.as_of) != parse_date(right.as_of):
        return None
    if left.polarity != right.polarity:
        return f"opposite assertions about {left.attribute}"
    left_values, right_values = _numbers(left.value), _numbers(right.value)
    if not left_values or not right_values:
        return None
    if left.unit and right.unit and compact(left.unit) != compact(right.unit):
        return None
    # A value inside (or within tolerance of) another source's range is not a disagreement.
    low_left, high_left = min(left_values), max(left_values)
    low_right, high_right = min(right_values), max(right_values)
    slack = NUMERIC_CONFLICT_TOLERANCE * max(abs(high_left), abs(high_right), 1e-9)
    if low_left - slack <= high_right and low_right - slack <= high_left:
        return None
    return f"different values for {left.attribute}: {left.value} vs {right.value}"


def _bm25(
    query_tokens: list[str], chunks: list[Chunk], k1: float = 1.2, b: float = 0.75
) -> dict[str, float]:
    documents = {
        chunk.id: tokens(f"{' '.join(chunk.heading_path)} {chunk.text}") for chunk in chunks
    }
    if not documents:
        return {}
    average = sum(len(words) for words in documents.values()) / len(documents) or 1.0
    frequency: Counter[str] = Counter()
    for words in documents.values():
        frequency.update(set(words))
    total = len(documents)
    scores: dict[str, float] = {}
    unique_query = set(query_tokens)
    for chunk_id, words in documents.items():
        counts = Counter(words)
        score = 0.0
        for term in unique_query:
            if term not in counts:
                continue
            idf = math.log(1 + (total - frequency[term] + 0.5) / (frequency[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(words) / average))
        scores[chunk_id] = score
    return scores


def _question_payload(plan: ResearchPlan, target_id: str) -> dict:
    """What the judge needs to know about a target. Never the planner's guesses."""
    target = plan.target(target_id)
    if target is None:
        return {"id": target_id}
    dimension = plan.dimension(target.dimension_id)
    return {
        "id": target.id,
        "text": target.text,
        "dimension": dimension.name if dimension else "",
        "scope": target.scope_note,
        "entity_ids": target.subject_ids,
        "kind_of_evidence": target.profile.authority_kind.value,
    }


def _chunk_payload(pool: EvidencePool, chunk_id: str, question_ids: list[str]) -> dict:
    chunk = pool.chunks[chunk_id]
    return {
        "chunk_id": chunk.id,
        "source_id": chunk.source_id,
        "question_ids": question_ids,
        "section": " > ".join(chunk.heading_path)[:200],
        "sentences": [[number, text] for number, text in enumerate(split_sentences(chunk.text), 1)],
    }


def _source_payload(source: ResearchSource) -> dict:
    return {
        "source_id": source.id,
        "title": source.title[:160],
        "host": source.host,
        "site_name": source.site_name[:120],
        "organizations": source.organization_names[:3],
        "published": source.published_date,
        "document": source.document_kind,
    }


def _attribute_registry(
    existing: list[EvidenceItem], question_ids: set[str]
) -> dict[str, list[str]]:
    """Attribute names already used per question, so the same property keeps one name."""
    registry: dict[str, list[str]] = {}
    for item in existing:
        if item.status == "accepted" and item.attribute and item.question_id in question_ids:
            names = registry.setdefault(item.question_id, [])
            if item.attribute not in names and len(names) < 30:
                names.append(item.attribute)
    return registry


def _cited_sentences(chunk: Chunk, sentence_ids: list[int]) -> list[str]:
    """The chunk sentences a finding cites, or nothing when any number names no sentence."""
    if not sentence_ids:
        return []
    sentences = split_sentences(chunk.text)
    wanted = list(dict.fromkeys(sentence_ids))
    if any(not 1 <= number <= len(sentences) for number in wanted):
        return []
    return [sentences[number - 1] for number in sorted(wanted)]


def _lead_key(lead: OriginLead) -> tuple[str, str]:
    return (lead.target_id, lead.href or compact(f"{lead.title} {lead.issuer}"))


def _grounded_lead(
    raw: _Lead, item: EvidenceItem, chunk: Chunk, source: ResearchSource, number: int
) -> OriginLead | None:
    """A cited originating record, kept only when the chunk itself names it.

    The title or issuer must appear in the chunk's own text, so a model cannot invent a record
    to follow. A link is attached only when one in the same part of the page carries the
    record's title or issuer: never a URL the model wrote.
    """
    title = normalize_ws(raw.title)[:300]
    issuer = normalize_ws(raw.issuer)[:120]
    text = grounding_form(chunk.text).lower()
    named = [value for value in (title, issuer) if value and grounding_form(value).lower() in text]
    if not named:
        return None
    wanted = token_set(f"{title} {issuer}")
    nearby = [
        link
        for link in source.links
        if link.block_index is not None and chunk.block_start <= link.block_index <= chunk.block_end
    ]
    scored = sorted(
        ((len(token_set(link.text) & wanted), index) for index, link in enumerate(nearby)),
        reverse=True,
    )
    href = nearby[scored[0][1]].href if scored and scored[0][0] >= 1 else None
    year = re.search(r"\b(1[5-9]\d\d|2\d\d\d)\b", raw.year or "")
    return OriginLead(
        id=f"lead{number}",
        target_id=item.question_id,
        kind=LeadKind.CITATION,
        from_source_id=source.id,
        independent_of=source.independence_key,
        grounding_sentence_ids=list(item.sentence_ids),
        title=title if title in named else "",
        issuer=issuer if issuer in named else "",
        year=year.group(1) if year else "",
        href=href,
    )


def _enum(enum_type, value: str | None, default):
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError:
        return default


def _short(value: str | None, limit: int = 160) -> str | None:
    cleaned = normalize_ws(value or "")[:limit]
    return cleaned or None
