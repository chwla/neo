"""Evidence extraction, quality scoring, entity-relevance filtering, gap detection, and deduplication."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

from app.services.research.types import (
    ResearchEvidenceChunk,
    ResearchPlan,
    ResearchSource,
)
from app.services.research.topic_intent import (
    TOPIC_AI_CODING_TOOLS,
    TopicIntent,
    ai_coding_entity_terms,
    classify_evidence_category,
    classify_topic_intent,
    is_low_quality_ai_coding_source,
    is_preferred_ai_coding_source,
    source_is_offtopic_for_ai_coding,
)
from app.services.research.product_intent import (
    TOPIC_PRODUCT_COMPARISON,
    classify_product_evidence_category,
    intent_from_topic,
    is_low_quality_product_source,
    is_preferred_product_source,
    product_entity_terms,
    source_is_offtopic_for_product,
)

logger = logging.getLogger(__name__)

_JUNK_PATTERNS = re.compile(
    r"(sign in|log in|create account|subscribe|cookie|privacy policy|"
    r"terms of service|accept cookies|enable javascript|"
    r"advertisement|sponsored|click here|buy now|free trial|"
    r"navigation|breadcrumb|skip to content|toggle menu|"
    r"share on twitter|share on facebook|follow us)",
    re.IGNORECASE,
)

_MIN_CHUNK_LENGTH = 40
_MAX_CHUNK_LENGTH = 800
_MIN_QUALITY_SCORE = 2.0


def extract_entity_terms(user_query: str, plan: ResearchPlan) -> list[str]:
    """Extract key entity terms that evidence chunks MUST contain at least one of."""
    if plan.topic_intent == TOPIC_AI_CODING_TOOLS:
        intent = classify_topic_intent(user_query, original_query=plan.original_query or user_query)
        if intent:
            return ai_coding_entity_terms(intent)

    if plan.topic_intent == TOPIC_PRODUCT_COMPARISON:
        intent = classify_topic_intent(user_query, original_query=plan.original_query or user_query)
        if intent:
            return product_entity_terms(intent_from_topic(intent))

    if plan.comparison_query and plan.normalized_entities:
        terms: list[str] = []
        for entity in plan.normalized_entities.values():
            terms.extend(_entity_aliases(entity))
        return list(dict.fromkeys(t for t in terms if t))

    query_lower = user_query.lower()
    objective_lower = plan.objective.lower()
    combined = query_lower + " " + objective_lower

    entity_groups: list[list[str]] = []

    if "amazing spider-man" in combined or "amazing spiderman" in combined:
        entity_groups.append([
            "amazing spider-man", "the amazing spider-man",
            "spider-man", "peter parker", "marvel comics",
            "stan lee", "steve ditko",
        ])
    elif "spider-man" in combined or "spiderman" in combined:
        entity_groups.append(["spider-man", "spiderman", "peter parker", "marvel"])
    elif "batman" in combined:
        entity_groups.append(["batman", "bruce wayne", "dc comics", "gotham"])
    elif "superman" in combined:
        entity_groups.append(["superman", "clark kent", "dc comics", "krypton"])

    if not entity_groups:
        words = set(re.findall(r"[a-z0-9][a-z0-9+.-]*", query_lower)) - {
            "research", "the", "a", "an", "of", "for", "and", "or", "to",
            "in", "on", "with", "about", "how", "what", "why", "best",
            "should", "compare", "comparison", "compared", "versus", "vs",
            "difference", "between", "current", "latest",
        }
        significant = [
            w for w in words
            if len(w) > 3 or (len(w) >= 2 and any(ch.isdigit() for ch in w))
        ]
        if significant:
            entity_groups.append(significant)

    return entity_groups[0] if entity_groups else []


def source_passes_entity_filter(
    source: ResearchSource,
    entity_terms: list[str],
) -> bool:
    """Check if a source's content is relevant to the research entity."""
    if not entity_terms:
        return True

    searchable = (source.title + " " + source.text[:3000]).lower()
    return any(_contains_entity_term(searchable, term) for term in entity_terms)


def filter_irrelevant_sources(
    sources: list[ResearchSource],
    entity_terms: list[str],
    plan: ResearchPlan | None = None,
    user_query: str = "",
) -> list[ResearchSource]:
    """Mark sources that don't mention the entity as rejected."""
    intent: TopicIntent | None = None
    product_intent = None
    orig = plan.original_query or user_query if plan else user_query
    if plan and plan.topic_intent == TOPIC_AI_CODING_TOOLS:
        intent = classify_topic_intent(user_query, original_query=orig)
    elif plan and plan.topic_intent == TOPIC_PRODUCT_COMPARISON:
        intent = classify_topic_intent(user_query, original_query=orig)
        if intent:
            product_intent = intent_from_topic(intent)

    if not entity_terms and not intent:
        return sources

    _IRRELEVANT_DOMAINS = {
        "vocabulary.com", "dictionary.com", "merriam-webster.com",
        "thesaurus.com", "wordreference.com", "collinsdictionary.com",
        "cambridge.org", "oxfordlearnersdictionaries.com",
        "yourdictionary.com", "urbandictionary.com",
    }
    _IRRELEVANT_TITLE_PATTERNS = re.compile(
        r"(meaning\s+in\s+english|definition\s+of|"
        r"what\s+does\s+\w+\s+mean|english\s+explained|"
        r"synonym|antonym|pronunciation|translate)",
        re.IGNORECASE,
    )

    for src in sources:
        if not src.fetched or not src.text:
            continue

        if intent and plan and plan.topic_intent == TOPIC_AI_CODING_TOOLS:
            reject_reason = source_is_offtopic_for_ai_coding(src, intent)
            if reject_reason:
                src.fetched = False
                src.fetch_status = "rejected"
                src.fetch_error = reject_reason
                src.text = ""
                continue
            if is_low_quality_ai_coding_source(src) and not is_preferred_ai_coding_source(src):
                src.quality_score = max(0.0, src.quality_score - 2.0)

        if product_intent:
            reject_reason = source_is_offtopic_for_product(src, product_intent)
            if reject_reason:
                src.fetched = False
                src.fetch_status = "rejected"
                src.fetch_error = reject_reason
                src.text = ""
                continue
            if is_low_quality_product_source(src) and not is_preferred_product_source(src, product_intent):
                src.quality_score = max(0.0, src.quality_score - 2.0)
            elif is_preferred_product_source(src, product_intent):
                src.quality_score = min(10.0, src.quality_score + 1.0)

        if plan and plan.domain_hint == "operating_system" and plan.comparison_query:
            reject_reason = _source_is_offtopic_for_os_comparison(src, plan)
            if reject_reason:
                src.fetched = False
                src.fetch_status = "rejected"
                src.fetch_error = reject_reason
                src.text = ""
                continue
            if _is_preferred_os_source(src, plan):
                src.quality_score = min(10.0, src.quality_score + 1.5)
            elif _is_low_quality_os_source(src, plan):
                src.quality_score = max(0.0, src.quality_score - 2.5)

        domain_lower = src.domain.lower()
        if any(d in domain_lower for d in _IRRELEVANT_DOMAINS):
            src.fetched = False
            src.fetch_status = "rejected"
            src.fetch_error = "Irrelevant domain (dictionary/language site)"
            src.text = ""
            continue

        if _IRRELEVANT_TITLE_PATTERNS.search(src.title):
            if not source_passes_entity_filter(src, entity_terms):
                src.fetched = False
                src.fetch_status = "rejected"
                src.fetch_error = "Irrelevant title (word definition, not research subject)"
                src.text = ""
                continue

        if not source_passes_entity_filter(src, entity_terms):
            src.fetched = False
            src.fetch_status = "rejected"
            src.fetch_error = "Source content does not mention the research subject"
            src.text = ""

    return sources


def extract_evidence(
    sources: list[ResearchSource],
    plan: ResearchPlan,
    entity_terms: list[str] | None = None,
    user_query: str = "",
) -> list[ResearchEvidenceChunk]:
    all_chunks: list[ResearchEvidenceChunk] = []
    seen_hashes: set[str] = set()
    intent: TopicIntent | None = None
    product_intent = None
    orig = plan.original_query or user_query
    if plan.topic_intent == TOPIC_AI_CODING_TOOLS:
        intent = classify_topic_intent(user_query, original_query=orig)
    elif plan.topic_intent == TOPIC_PRODUCT_COMPARISON:
        intent = classify_topic_intent(user_query, original_query=orig)
        if intent:
            product_intent = intent_from_topic(intent)

    for source in sources:
        if not source.fetched or not source.text:
            continue
        chunks = _extract_from_source(
            source, plan, entity_terms or [], intent, product_intent,
        )
        for chunk in chunks:
            h = _chunk_hash(chunk.text)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            all_chunks.append(chunk)

    all_chunks.sort(key=lambda c: (c.quality_score + c.relevance_score), reverse=True)
    return all_chunks


def identify_gaps(
    plan: ResearchPlan,
    evidence: list[ResearchEvidenceChunk],
    user_query: str = "",
) -> list[str]:
    gaps: list[str] = []
    covered_subquestions: set[str] = set()

    for chunk in evidence:
        if chunk.supports_subquestion:
            covered_subquestions.add(chunk.supports_subquestion)

    for subq in plan.subquestions:
        if subq not in covered_subquestions:
            has_partial = any(
                _text_overlap(subq, chunk.text) > 0.2
                for chunk in evidence
            )
            if not has_partial:
                gaps.append(f"Unanswered: {subq}")

    if plan.topic_intent == TOPIC_PRODUCT_COMPARISON and plan.comparison_tools:
        air_ev = [c for c in evidence if c.evidence_category in ("air_evidence", "comparison_evidence")]
        pro_ev = [c for c in evidence if c.evidence_category in ("pro_evidence", "comparison_evidence")]
        if "macbook air" in (plan.comparison_tools or []) and not air_ev:
            gaps.append("Missing MacBook Air-specific evidence")
        if "macbook pro" in (plan.comparison_tools or []) and not pro_ev:
            gaps.append("Missing MacBook Pro-specific evidence")
        if plan.comparison_query and not any(
            c.evidence_category == "comparison_evidence" for c in evidence
        ):
            gaps.append("Missing direct comparison evidence")

    if plan.topic_intent == TOPIC_AI_CODING_TOOLS:
        tools = plan.comparison_tools or []
        tool_evidence: dict[str, list] = {t: [] for t in tools}
        for chunk in evidence:
            cat = chunk.evidence_category
            if cat == "comparison_evidence":
                for t in tools:
                    tool_evidence.setdefault(t, []).append(chunk)
            elif cat.endswith("_evidence"):
                slug = cat.replace("_evidence", "")
                if slug in tool_evidence:
                    tool_evidence[slug].append(chunk)
        for tool in tools:
            if not tool_evidence.get(tool):
                label = plan.normalized_entities.get(tool, tool)
                gaps.append(f"Missing {label}-specific evidence")
        if not any(c.evidence_category == "comparison_evidence" for c in evidence):
            gaps.append("Missing direct comparison evidence")

    if (
        plan.comparison_query
        and plan.normalized_entities
        and plan.topic_intent not in (TOPIC_PRODUCT_COMPARISON, TOPIC_AI_CODING_TOOLS)
    ):
        entities = list(plan.normalized_entities.values())
        left_label = entities[0] if entities else "left entity"
        right_label = entities[1] if len(entities) > 1 else "right entity"
        left_ev = [c for c in evidence if c.evidence_category in ("left_evidence", "comparison_evidence")]
        right_ev = [c for c in evidence if c.evidence_category in ("right_evidence", "comparison_evidence")]
        if not left_ev:
            gaps.append(f"Missing {left_label}-specific evidence")
        if not right_ev:
            gaps.append(f"Missing {right_label}-specific evidence")
        if not any(c.evidence_category == "comparison_evidence" for c in evidence):
            gaps.append("Missing direct comparison evidence")

    if len(evidence) < 3:
        gaps.append("Weak evidence: fewer than 3 evidence chunks found")

    unique_sources = {c.source_url for c in evidence}
    if len(unique_sources) < 2:
        gaps.append("Limited sources: evidence comes from fewer than 2 sources")

    contradictions = _find_contradictions(evidence)
    for c in contradictions:
        gaps.append(f"Contradiction: {c}")

    return gaps


def _extract_from_source(
    source: ResearchSource,
    plan: ResearchPlan,
    entity_terms: list[str],
    intent: TopicIntent | None = None,
    product_intent=None,
) -> list[ResearchEvidenceChunk]:
    text = source.text
    paragraphs = _split_into_paragraphs(text)
    chunks: list[ResearchEvidenceChunk] = []
    now = datetime.now(timezone.utc).isoformat()

    for para in paragraphs:
        if len(para) < _MIN_CHUNK_LENGTH:
            continue
        if _JUNK_PATTERNS.search(para):
            continue

        if entity_terms and not _chunk_has_entity_relevance(para, entity_terms):
            continue

        evidence_category = "general"
        quality_boost = 0.0
        if product_intent:
            evidence_category = classify_product_evidence_category(para, source, product_intent)
            if evidence_category == "irrelevant":
                continue
            if is_preferred_product_source(source, product_intent):
                quality_boost = 1.5
        elif intent and plan.topic_intent == TOPIC_AI_CODING_TOOLS:
            evidence_category = classify_evidence_category(para, source, intent)
            if evidence_category == "irrelevant":
                continue
            if is_preferred_ai_coding_source(source):
                quality_boost = 1.5
        elif plan.comparison_query and plan.normalized_entities:
            evidence_category = _classify_generic_comparison_evidence(para, source, plan)
            if evidence_category == "irrelevant":
                continue
            if plan.domain_hint == "operating_system" and _is_preferred_os_source(source, plan):
                quality_boost = 1.5

        relevance = _score_relevance(para, plan)
        quality = _score_chunk_quality(para, source) + quality_boost

        if quality < _MIN_QUALITY_SCORE:
            continue

        subq = _find_supporting_subquestion(para, plan)
        claim_type = _classify_claim(para)

        chunks.append(ResearchEvidenceChunk(
            source_id=source.id,
            source_url=source.url,
            source_title=source.title,
            text=para[:_MAX_CHUNK_LENGTH],
            relevance_score=relevance,
            quality_score=quality,
            claim_type=claim_type,
            evidence_category=evidence_category,
            supports_subquestion=subq,
            extracted_at=now,
        ))

    chunks.sort(key=lambda c: c.relevance_score + c.quality_score, reverse=True)
    return chunks[:8]


def _chunk_has_entity_relevance(text: str, entity_terms: list[str]) -> bool:
    """A chunk must mention at least one entity term to be considered relevant."""
    if not entity_terms:
        return True
    text_lower = text.lower()
    return any(_contains_entity_term(text_lower, term) for term in entity_terms)


def _comparison_entities(plan: ResearchPlan) -> tuple[str, str] | None:
    if not plan.comparison_query or not plan.normalized_entities:
        return None
    entities = list(plan.normalized_entities.values())
    if len(entities) < 2:
        return None
    return entities[0], entities[1]


def _entity_aliases(entity: str) -> list[str]:
    entity = entity.strip()
    if not entity:
        return []
    aliases = [entity.lower()]
    entity_lower = entity.lower()
    if entity_lower.startswith("macos"):
        aliases.append("mac os")
    if entity_lower == "c++":
        aliases.append("cplusplus")
    return aliases


def _contains_entity_term(text_lower: str, term: str) -> bool:
    term_lower = term.lower().strip()
    if not term_lower:
        return False
    if len(term_lower) <= 2 or re.search(r"[+#.]", term_lower):
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(term_lower)}(?![a-z0-9])", text_lower))
    return term_lower in text_lower


def _source_mentions_entity(source: ResearchSource, entity: str) -> bool:
    searchable = (source.title + " " + source.text[:5000]).lower()
    return any(_contains_entity_term(searchable, alias) for alias in _entity_aliases(entity))


def _text_mentions_entity(text: str, entity: str) -> bool:
    text_lower = text.lower()
    return any(_contains_entity_term(text_lower, alias) for alias in _entity_aliases(entity))


def _classify_generic_comparison_evidence(
    text: str,
    source: ResearchSource,
    plan: ResearchPlan,
) -> str:
    pair = _comparison_entities(plan)
    if not pair:
        return "general"
    left, right = pair
    combined = f"{source.title} {text}"
    has_left = _text_mentions_entity(combined, left)
    has_right = _text_mentions_entity(combined, right)
    if has_left and has_right:
        return "comparison_evidence"
    if has_left:
        return "left_evidence"
    if has_right:
        return "right_evidence"
    return "irrelevant"


def _official_os_domains_for_entity(entity: str) -> tuple[str, ...]:
    entity_lower = entity.lower()
    if entity_lower == "ubuntu":
        return ("ubuntu.com", "help.ubuntu.com", "canonical.com")
    if entity_lower == "linux mint":
        return ("linuxmint.com",)
    if entity_lower == "fedora":
        return ("fedoraproject.org", "docs.fedoraproject.org")
    if entity_lower in {"arch", "arch linux"}:
        return ("archlinux.org", "wiki.archlinux.org")
    if entity_lower == "manjaro":
        return ("manjaro.org",)
    if entity_lower == "debian":
        return ("debian.org",)
    if entity_lower == "windows":
        return ("microsoft.com", "learn.microsoft.com")
    if entity_lower in {"macos", "mac os"}:
        return ("apple.com", "support.apple.com")
    return ()


def _is_preferred_os_source(source: ResearchSource, plan: ResearchPlan) -> bool:
    pair = _comparison_entities(plan)
    if not pair:
        return False
    domain = (source.domain or "").lower()
    return any(
        any(official in domain for official in _official_os_domains_for_entity(entity))
        for entity in pair
    )


def _is_low_quality_os_source(source: ResearchSource, plan: ResearchPlan) -> bool:
    domain = (source.domain or "").lower()
    title = (source.title or "").lower()
    if "geeksforgeeks.org" in domain and re.search(r"\b(introduction|tutorial|what is|linux/unix)\b", title):
        return True
    if "wikipedia.org" in domain and re.search(r"^(linux|operating system|unix)", title):
        return True
    if "apps.microsoft.com" in domain or "microsoft.com/store" in source.url.lower():
        query = f"{plan.original_query or ''} {plan.normalized_query or ''}".lower()
        return "wsl" not in query and "store" not in query and "windows install" not in query
    return False


def _source_is_offtopic_for_os_comparison(source: ResearchSource, plan: ResearchPlan) -> str | None:
    pair = _comparison_entities(plan)
    if not pair:
        return None
    left, right = pair
    title = (source.title or "").lower()
    searchable = (source.title + " " + source.text[:5000]).lower()

    fake_pairs = [
        f"{left.split()[0]} {right.split()[-1]}".lower(),
        f"{right.split()[0]} {left.split()[-1]}".lower(),
    ]
    if any(fake in title for fake in fake_pairs if fake not in {left.lower(), right.lower()}):
        return "Wrong merged comparison entity"

    mentions_left = _source_mentions_entity(source, left)
    mentions_right = _source_mentions_entity(source, right)
    if not mentions_left and not mentions_right:
        return "Source content does not mention either compared operating system"

    if _is_low_quality_os_source(source, plan):
        return "Low-quality or generic operating system source"

    if re.search(r"\b(linux/unix tutorial|introduction to linux|linux tutorial)\b", searchable):
        if left.lower() != "linux" and right.lower() != "linux":
            return "Generic Linux tutorial, not the compared distributions"

    return None


def _find_contradictions(evidence: list[ResearchEvidenceChunk]) -> list[str]:
    contradictions: list[str] = []
    negative_words = {"not", "no", "never", "cannot", "doesn't", "don't", "isn't", "won't", "lack", "lacks", "without"}

    claims_by_subq: dict[str, list[ResearchEvidenceChunk]] = {}
    for chunk in evidence:
        key = chunk.supports_subquestion or "general"
        claims_by_subq.setdefault(key, []).append(chunk)

    for subq, chunks in claims_by_subq.items():
        if len(chunks) < 2:
            continue
        for i, a in enumerate(chunks):
            for b in chunks[i + 1:]:
                if a.source_url == b.source_url:
                    continue
                a_neg = sum(1 for w in a.text.lower().split() if w in negative_words)
                b_neg = sum(1 for w in b.text.lower().split() if w in negative_words)
                if abs(a_neg - b_neg) >= 3:
                    contradictions.append(
                        f"Sources disagree on '{subq}': {a.source_title} vs {b.source_title}"
                    )
                    if len(contradictions) >= 3:
                        return contradictions
    return contradictions


def _split_into_paragraphs(text: str) -> list[str]:
    raw_blocks = re.split(r"\n\s*\n|\n(?=[A-Z])", text)
    paragraphs: list[str] = []
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) > _MAX_CHUNK_LENGTH * 2:
            sentences = re.split(r"(?<=[.!?])\s+", block)
            current = ""
            for sent in sentences:
                if len(current) + len(sent) > _MAX_CHUNK_LENGTH:
                    if current:
                        paragraphs.append(current.strip())
                    current = sent
                else:
                    current = f"{current} {sent}" if current else sent
            if current:
                paragraphs.append(current.strip())
        else:
            paragraphs.append(block)
    return paragraphs


def _score_relevance(text: str, plan: ResearchPlan) -> float:
    score = 0.0
    text_lower = text.lower()

    objective_words = set(plan.objective.lower().split())
    objective_words -= {"the", "a", "an", "is", "are", "of", "for", "and", "or", "to", "in", "on", "with"}
    if objective_words:
        hits = sum(1 for w in objective_words if w in text_lower)
        score += (hits / len(objective_words)) * 4.0

    for subq in plan.subquestions:
        subq_words = set(subq.lower().split()) - {"what", "how", "which", "does", "is", "are", "the", "a"}
        if subq_words:
            hits = sum(1 for w in subq_words if w in text_lower)
            if hits >= len(subq_words) * 0.4:
                score += 1.5

    for query in plan.queries[:5]:
        q_words = set(query.lower().split()) - {"the", "a", "vs", "and", "or", "for"}
        if q_words:
            hits = sum(1 for w in q_words if w in text_lower)
            if hits >= len(q_words) * 0.5:
                score += 0.5

    return min(10.0, score)


def _score_chunk_quality(text: str, source: ResearchSource) -> float:
    score = source.quality_score * 0.3

    if any(c.isdigit() for c in text):
        score += 1.0
    if re.search(r"\d{4}", text):
        score += 0.5
    if re.search(r"\$[\d,.]+|€[\d,.]+|\d+\s*(GB|MB|TB|GHz|MHz|RAM|VRAM)", text, re.IGNORECASE):
        score += 1.5

    word_count = len(text.split())
    if 20 < word_count < 150:
        score += 1.0
    elif word_count >= 150:
        score += 0.5

    if text.count(".") >= 2:
        score += 0.5

    if _JUNK_PATTERNS.search(text):
        score -= 3.0

    return max(0.0, min(10.0, score))


def _find_supporting_subquestion(text: str, plan: ResearchPlan) -> str | None:
    best_match = None
    best_score = 0.0
    text_lower = text.lower()

    for subq in plan.subquestions:
        overlap = _text_overlap(subq, text_lower)
        if overlap > best_score and overlap > 0.3:
            best_score = overlap
            best_match = subq

    return best_match


def _classify_claim(text: str) -> str:
    text_lower = text.lower()
    if re.search(r"\$[\d,.]+|price|cost|pricing|free tier|subscription", text_lower):
        return "pricing"
    if re.search(r"\d+\s*(GB|MB|TB|GHz|cores?|threads?|VRAM|RAM)", text_lower, re.IGNORECASE):
        return "specification"
    if re.search(r"(better|worse|faster|slower|compared to|versus|vs\.?|unlike)", text_lower):
        return "comparison"
    if re.search(r"(recommend|should|best|ideal|prefer)", text_lower):
        return "recommendation"
    if re.search(r"(risk|warning|caution|limitation|drawback|downside|caveat)", text_lower):
        return "risk"
    if re.search(r"(released|version|v\d|\d+\.\d+|first\s+appear|issue\s+#?\d|published)", text_lower):
        return "version_info"
    return "general"


def _text_overlap(a: str, b: str) -> float:
    words_a = set(a.lower().split()) - {"what", "how", "which", "does", "is", "are", "the", "a", "an", "of", "for"}
    words_b = set(b.lower().split())
    if not words_a:
        return 0.0
    return len(words_a & words_b) / len(words_a)


def _chunk_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.lower().strip())[:200]
    return hashlib.md5(normalized.encode()).hexdigest()
