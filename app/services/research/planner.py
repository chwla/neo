"""Research planner: converts user query into a structured research plan via LLM."""

from __future__ import annotations

import json
import logging
import re

from app.services.ollama_client import OllamaClient, OllamaMessage
from app.services.research.topic_intent import (
    TOPIC_AI_CODING_TOOLS,
    TopicIntent,
    build_ai_coding_plan,
    classify_topic_intent,
    filter_offtopic_ai_coding_queries,
    is_offtopic_ai_coding_query,
)
from app.services.research.product_intent import (
    TOPIC_PRODUCT_COMPARISON,
    filter_offtopic_product_queries,
    build_product_plan,
    is_offtopic_product_query,
    intent_from_topic,
)
from app.services.research.types import DepthMode, DEPTH_CONFIG, ResearchPlan

logger = logging.getLogger(__name__)

PLANNING_SYSTEM_PROMPT = """\
You are a research planner. Given a user's research question, produce a JSON research plan.

Output ONLY valid JSON with this structure:
{
  "objective": "one sentence describing the research goal — include the FULL entity name",
  "subquestions": ["list of 3-6 specific sub-questions to investigate"],
  "queries": ["list of web search queries to run"],
  "freshness_required": true or false,
  "source_preferences": ["types of sources to prefer, e.g. official docs, GitHub, pricing pages"],
  "expected_output": "comparison" or "recommendation" or "overview" or "analysis"
}

CRITICAL Rules for query generation:
- ALWAYS preserve the FULL entity name in every search query.
  Example: for "amazing spiderman comics", every query must include "Amazing Spider-Man" or "The Amazing Spider-Man".
  NEVER generate queries like "amazing meaning" or "comics history" without the entity name.
- Add disambiguating context words: the publisher, creator names, category, official name.
  Example: "Amazing Spider-Man Marvel Comics", "The Amazing Spider-Man Stan Lee Steve Ditko"
- For media franchises (comics, movies, games, books), include the publisher/studio/author.
- For tech products, include the company name.
- Each query should be a realistic web search query (short, keyword-focused).
- Generate diverse queries covering different aspects of the topic.
- Sub-questions should break the research into answerable parts.
- Set freshness_required=true if the topic involves current/recent information.
- Do NOT include any text outside the JSON object.
"""


def generate_plan(
    user_query: str,
    depth: DepthMode = DepthMode.STANDARD,
    memory_context: str = "",
    ollama: OllamaClient | None = None,
    topic_intent: TopicIntent | None = None,
    original_query: str = "",
) -> ResearchPlan:
    config = DEPTH_CONFIG[depth]
    orig = original_query or user_query
    intent = topic_intent or classify_topic_intent(user_query, original_query=orig)

    if intent and intent.topic_intent == TOPIC_PRODUCT_COMPARISON:
        return _product_comparison_plan(user_query, config, intent, orig)

    if intent and intent.topic_intent == TOPIC_AI_CODING_TOOLS:
        return _ai_coding_tools_plan(user_query, config, intent)

    comparison = _extract_generic_comparison(user_query)
    if comparison:
        return _generic_comparison_plan(user_query, config, comparison, orig)

    client = ollama or OllamaClient(num_predict=512)
    entity_hint = _extract_entity_hint(user_query)

    user_content = f"Research question: {user_query}"
    if entity_hint:
        user_content += f"\n\nEntity disambiguation: the main subject is \"{entity_hint}\". All search queries MUST include this entity name."
    if memory_context:
        user_content += f"\n\nUser context (use for personalization only):\n{memory_context}"
    user_content += f"\n\nGenerate {config['min_queries']}-{config['max_queries']} search queries."

    messages = [
        OllamaMessage(role="system", content=PLANNING_SYSTEM_PROMPT),
        OllamaMessage(role="user", content=user_content),
    ]

    try:
        raw = client.chat(messages, temperature=0.3)
        plan = _parse_plan_json(raw, config)
        if plan and plan.queries:
            plan.queries = _anchor_queries(plan.queries, user_query, entity_hint)
            return plan
        logger.warning("LLM plan parsing failed or empty, using fallback")
    except Exception:
        logger.exception("LLM planning failed")

    return _fallback_plan(user_query, config, entity_hint)


def _product_comparison_plan(
    user_query: str, config: dict, intent: TopicIntent, original_query: str,
) -> ResearchPlan:
    product = intent_from_topic(intent)
    payload = build_product_plan(product, user_query)
    queries = filter_offtopic_product_queries(payload["queries"])
    queries = queries[: config["max_queries"]]
    if len(queries) < config["min_queries"]:
        for q in build_product_plan(product, user_query)["queries"]:
            if q not in queries and not is_offtopic_product_query(q):
                queries.append(q)
            if len(queries) >= config["min_queries"]:
                break
    return ResearchPlan(
        objective=payload["objective"],
        subquestions=payload["subquestions"],
        queries=queries,
        freshness_required=payload["freshness_required"],
        source_preferences=payload["source_preferences"],
        expected_output=payload["expected_output"],
        topic_intent=intent.topic_intent,
        normalized_entities=intent.normalized_entities,
        comparison_tools=intent.tools,
        original_query=original_query,
        normalized_query=intent.normalized_query,
        normalization_reason=intent.normalization_reason,
        ai_workload_focus=intent.ai_workload_focus,
        product_pair=intent.product_pair,
        comparison_query=intent.comparison_query,
    )


def _ai_coding_tools_plan(user_query: str, config: dict, intent: TopicIntent) -> ResearchPlan:
    payload = build_ai_coding_plan(intent, user_query)
    queries = filter_offtopic_ai_coding_queries(payload["queries"])
    queries = queries[: config["max_queries"]]
    if len(queries) < config["min_queries"]:
        extra = build_ai_coding_plan(intent, user_query)["queries"]
        for q in extra:
            if q not in queries and not is_offtopic_ai_coding_query(q):
                queries.append(q)
            if len(queries) >= config["min_queries"]:
                break
    return ResearchPlan(
        objective=payload["objective"],
        subquestions=payload["subquestions"],
        queries=queries,
        freshness_required=payload["freshness_required"],
        source_preferences=payload["source_preferences"],
        expected_output="comparison",
        topic_intent=intent.topic_intent,
        normalized_entities=intent.normalized_entities,
        comparison_tools=intent.tools,
        comparison_query=intent.comparison_query,
    )


def _generic_comparison_plan(
    user_query: str,
    config: dict,
    comparison: dict[str, str],
    original_query: str,
) -> ResearchPlan:
    """Deterministic, topic-agnostic plan for comparison queries."""
    left = comparison["left"]
    right = comparison["right"]
    context = comparison.get("context", "")
    domain_hint = comparison.get("domain_hint", "")
    qualifiers = comparison.get("qualifiers", [])
    pair = f"{left} vs {right}"
    if context and context.lower() not in pair.lower():
        pair_with_context = f"{pair} {context}"
    else:
        pair_with_context = pair

    subquestions = [
        f"What is the exact scope of {left} and {right} in this query?",
        f"What reliable sources establish the key facts about {left}?",
        f"What reliable sources establish the key facts about {right}?",
        f"What direct comparison evidence exists for {pair_with_context}?",
        "What are the tradeoffs, unknowns, and practical recommendation supported by evidence?",
    ]

    if domain_hint == "operating_system":
        source_preferences = [
            "official project websites and documentation",
            "official download, release, and support pages",
            "reputable Linux or desktop operating system review sources",
            "direct comparison articles as secondary sources",
        ]
        queries = [
            _entity_query(left, context),
            _entity_query(right, context),
            *_official_os_queries(left, right),
            f"{left} official documentation",
            f"{right} official documentation",
            f"{pair_with_context} comparison",
            f"{pair_with_context} desktop operating system comparison",
            f"{pair_with_context} personal use review",
            f"{pair_with_context} beginner desktop Linux",
        ]
    else:
        source_preferences = [
            "official or primary sources",
            "reputable publications and expert reviews",
            "independent tests, benchmarks, datasets, or direct comparisons when relevant",
        ]
        queries = [
            f"{pair_with_context} official",
            _entity_query(left, context),
            _entity_query(right, context),
            f"{pair_with_context} comparison",
            f"{pair_with_context} benchmark review",
            f"{pair_with_context} differences",
            f"{pair_with_context} buyer guide",
            f"{pair_with_context} expert review",
            f"{pair_with_context} real world test",
            f"{pair_with_context} pros cons",
        ]
    queries = list(dict.fromkeys(q for q in queries if len(q.strip()) > 3))[: config["max_queries"]]

    return ResearchPlan(
        objective=f"Compare {pair_with_context} using reliable internet sources.",
        subquestions=subquestions,
        queries=queries,
        freshness_required=True,
        source_preferences=source_preferences,
        expected_output="comparison",
        normalized_entities={"left": left, "right": right},
        comparison_tools=["left", "right"],
        original_query=original_query,
        normalized_query=pair_with_context,
        domain_hint=domain_hint or None,
        qualifiers=qualifiers,
        comparison_query=True,
    )


def generate_followup_queries(
    user_query: str,
    plan: ResearchPlan,
    gaps: list[str],
    ollama: OllamaClient | None = None,
) -> list[str]:
    if not gaps:
        return []

    if plan.topic_intent == TOPIC_PRODUCT_COMPARISON:
        intent = classify_topic_intent(user_query)
        if intent and intent.product_pair and "macbook" in (intent.product_pair or ""):
            followups: list[str] = []
            for gap in gaps[:3]:
                gl = gap.lower()
                if "air" in gl and "pro" not in gl:
                    followups.append("Apple MacBook Air official specs site:apple.com")
                elif "pro" in gl:
                    followups.append("Apple MacBook Pro official specs site:apple.com")
                elif "battery" in gl:
                    followups.append("MacBook Air vs MacBook Pro battery life comparison")
                elif "comparison" in gl:
                    followups.append("MacBook Air vs MacBook Pro comparison review")
                elif "pricing" in gl or "price" in gl:
                    followups.append("MacBook Air vs MacBook Pro pricing comparison")
                else:
                    followups.append("MacBook Air vs MacBook Pro ports display weight")
            return filter_offtopic_product_queries(list(dict.fromkeys(followups)))[:4]

    if plan.topic_intent == TOPIC_AI_CODING_TOOLS:
        intent = classify_topic_intent(user_query)
        if intent:
            followups: list[str] = []
            for gap in gaps[:3]:
                if "pricing" in gap.lower() or "price" in gap.lower():
                    followups.append("Cursor AI Pro vs OpenAI Codex Pro pricing comparison")
                elif "cursor" in gap.lower():
                    followups.append("Cursor AI editor official documentation agent features")
                elif "codex" in gap.lower():
                    followups.append("OpenAI Codex CLI cloud coding agent official docs")
                else:
                    followups.append(f"Cursor AI vs OpenAI Codex {gap.split()[-1]}")
            return filter_offtopic_ai_coding_queries(list(dict.fromkeys(followups)))[:4]

    entity_hint = _extract_entity_hint(user_query)
    client = ollama or OllamaClient(num_predict=256)
    prompt = (
        f"Original research question: {user_query}\n"
        f"Research objective: {plan.objective}\n"
    )
    if entity_hint:
        prompt += f"Main entity: {entity_hint}\n"
    prompt += (
        f"Gaps found after initial research:\n"
        + "\n".join(f"- {g}" for g in gaps)
        + "\n\nGenerate 2-4 follow-up web search queries to fill these gaps. "
        f"Every query MUST include the entity name \"{entity_hint or user_query}\". "
        "Output ONLY a JSON array of query strings."
    )

    try:
        raw = client.chat(
            [OllamaMessage(role="system", content="You generate web search queries. Output ONLY a JSON array of strings."),
             OllamaMessage(role="user", content=prompt)],
            temperature=0.3,
        )
        queries = _parse_json_array(raw)
        if queries:
            return _anchor_queries(queries[:4], user_query, entity_hint)
    except Exception:
        logger.exception("Follow-up query generation failed")

    anchor = entity_hint or user_query
    return [f"{anchor} {gap.split()[-1]}" for gap in gaps[:2]]


_GENERIC_COMPARISON_SPLIT = re.compile(
    r"\b(?:vs\.?|versus|compared\s+to|compare|comparison\s+of|or)\b",
    re.IGNORECASE,
)

_QUALIFIER_SPLIT = re.compile(
    r"\b(for|in|as|on|with|under|during|from)\b",
    re.IGNORECASE,
)

_OPERATING_SYSTEM_TERMS = {
    "ubuntu", "linux mint", "fedora", "arch", "arch linux", "manjaro",
    "windows", "linux", "macos", "mac os", "debian", "pop!_os", "pop os",
    "elementary os", "zorin os", "opensuse", "red hat", "rhel",
}

_ENTITY_CASE_OVERRIDES = {
    "c": "C",
    "c++": "C++",
    "c#": "C#",
    "go": "Go",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "python": "Python",
    "java": "Java",
    "rust": "Rust",
    "macbook": "MacBook",
    "macos": "macOS",
    "os": "OS",
    "ai": "AI",
    "ios": "iOS",
    "rhel": "RHEL",
    "pop!_os": "Pop!_OS",
    "chip": "chip",
}

_LOWERCASE_ENTITY_WORDS = {"in", "for", "on", "with", "and", "or", "of", "to"}


def _extract_generic_comparison(query: str) -> dict[str, str] | None:
    """Extract comparison sides without topic-specific knowledge."""
    q = re.sub(r"\s+", " ", query.strip())
    if not q:
        return None

    diff = re.search(
        r"\bdifference\s+between\s+(.+?)\s+and\s+(.+)$",
        q,
        flags=re.IGNORECASE,
    )
    if diff:
        left = diff.group(1).strip(" ?.,")
        right = diff.group(2).strip(" ?.,")
        return _normalize_comparison_parts(left, right)

    parts = _GENERIC_COMPARISON_SPLIT.split(q, maxsplit=1)
    if len(parts) == 2:
        return _normalize_comparison_parts(parts[0].strip(" ?.,:"), parts[1].strip(" ?.,:"))

    return None


def _normalize_comparison_parts(left: str, right: str) -> dict[str, str] | None:
    left = _clean_comparison_side(left)
    right = _clean_comparison_side(right)
    if not left or not right:
        return None

    right_entity, right_context = _split_entity_qualifier(right, left)
    left_entity, left_context = _split_entity_qualifier(left, right_entity)
    left = left_entity
    right = right_entity

    context = ""
    if right_context:
        context = right_context
    elif left_context:
        context = left_context

    if context and _should_share_context(context):
        if context.lower() not in left.lower():
            left = f"{left} {context}".strip()
        if context.lower() not in right.lower():
            right = f"{right} {context}".strip()

    left = _canonicalize_entity(left)
    right = _canonicalize_entity(right)
    context = _clean_comparison_side(context)
    domain_hint = _detect_comparison_domain(left, right, context)
    qualifiers = _extract_qualifiers(context)

    return {
        "left": left,
        "right": right,
        "context": context,
        "domain_hint": domain_hint,
        "qualifiers": qualifiers,
    }


def _clean_comparison_side(value: str) -> str:
    value = re.sub(r"^(the|a|an)\s+", "", value.strip(), flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" ?.,:")


def _should_share_context(context: str) -> bool:
    words = context.split()
    if not words:
        return False
    return bool(re.search(r"\b(in|on|with|under|during|from)\b", context, re.IGNORECASE))


def _entity_query(entity: str, context: str) -> str:
    if _should_share_context(context) and context.lower() not in entity.lower():
        return f"{entity} official {context}".strip()
    return f"{entity} official"


def _official_os_queries(left: str, right: str) -> list[str]:
    queries: list[str] = []
    for entity in (left, right):
        entity_lower = entity.lower()
        if entity_lower == "ubuntu":
            queries.append(f"site:ubuntu.com {entity} desktop official")
            queries.append(f"site:help.ubuntu.com {entity} documentation")
        elif entity_lower == "linux mint":
            queries.append(f"site:linuxmint.com {entity} official documentation")
            queries.append(f"site:linuxmint.com {entity} release notes")
        elif entity_lower == "fedora":
            queries.append(f"site:fedoraproject.org {entity} official documentation")
        elif entity_lower in {"arch", "arch linux"}:
            queries.append(f"site:archlinux.org {entity} official documentation")
        elif entity_lower == "manjaro":
            queries.append(f"site:manjaro.org {entity} official documentation")
        elif entity_lower == "debian":
            queries.append(f"site:debian.org {entity} official documentation")
        elif entity_lower == "windows":
            queries.append(f"site:microsoft.com {entity} official documentation")
        elif entity_lower in {"macos", "mac os"}:
            queries.append(f"site:apple.com {entity} official documentation")
    return queries


def _split_entity_qualifier(value: str, peer: str) -> tuple[str, str]:
    """Split a comparison side into the entity and a trailing use-case/domain qualifier."""
    match = _QUALIFIER_SPLIT.search(value)
    if not match:
        return value, ""

    before = value[:match.start()].strip()
    after = value[match.start():].strip()
    if not before or not after:
        return value, ""

    marker = match.group(1).lower()
    if marker == "for":
        return before, after

    before_words = before.split()
    peer_words = peer.split()
    if len(before_words) > len(peer_words) and len(peer_words) <= 2:
        descriptor = " ".join(before_words[len(peer_words):])
        entity = " ".join(before_words[:len(peer_words)])
        if descriptor:
            return entity, f"{descriptor} {after}".strip()

    return before, after


def _canonicalize_entity(value: str) -> str:
    words = value.split()
    fixed: list[str] = []
    for idx, word in enumerate(words):
        key = word.lower()
        if re.fullmatch(r"[a-zA-Z]\d+[a-zA-Z0-9-]*", word):
            fixed.append(word.upper())
        elif key in _ENTITY_CASE_OVERRIDES:
            fixed.append(_ENTITY_CASE_OVERRIDES[key])
        elif idx > 0 and key in _LOWERCASE_ENTITY_WORDS:
            fixed.append(key)
        else:
            fixed.append(word[:1].upper() + word[1:])
    return " ".join(fixed)


def _detect_comparison_domain(left: str, right: str, context: str) -> str:
    combined = f"{left} {right} {context}".lower()
    entities = {left.lower(), right.lower()}
    if entities & _OPERATING_SYSTEM_TERMS:
        return "operating_system"
    if "operating system" in combined or "linux distribution" in combined or "desktop linux" in combined:
        return "operating_system"
    return ""


def _extract_qualifiers(context: str) -> list[str]:
    context_lower = context.lower()
    qualifiers: list[str] = []
    if "personal use" in context_lower:
        qualifiers.append("personal use")
    if "beginner" in context_lower:
        qualifiers.append("beginner")
    if "desktop" in context_lower:
        qualifiers.append("desktop")
    if "programming" in context_lower:
        qualifiers.append("programming")
    return qualifiers


_ENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bamazing\s+spider[- ]?man\b", re.I), "The Amazing Spider-Man Marvel Comics"),
    (re.compile(r"\bspider[- ]?man\b", re.I), "Spider-Man Marvel"),
    (re.compile(r"\bbatman\b", re.I), "Batman DC Comics"),
    (re.compile(r"\bsuperman\b", re.I), "Superman DC Comics"),
    (re.compile(r"\bx[- ]?men\b", re.I), "X-Men Marvel Comics"),
    (re.compile(r"\bavengers\b", re.I), "Avengers Marvel"),
    (re.compile(r"\bjustice\s+league\b", re.I), "Justice League DC Comics"),
    (re.compile(r"\bstar\s+wars\b", re.I), "Star Wars Lucasfilm"),
    (re.compile(r"\bgame\s+of\s+thrones\b", re.I), "Game of Thrones HBO"),
    (re.compile(r"\bminecraft\b", re.I), "Minecraft Mojang"),
    (re.compile(r"\bfortnite\b", re.I), "Fortnite Epic Games"),
]


def _extract_entity_hint(query: str) -> str:
    """Detect known franchises/entities and return a disambiguation string."""
    for pattern, hint in _ENTITY_PATTERNS:
        if pattern.search(query):
            return hint
    return ""


def _anchor_queries(queries: list[str], user_query: str, entity_hint: str) -> list[str]:
    """Ensure every query contains the core entity terms."""
    if not entity_hint:
        return queries

    core_terms = _get_core_terms(entity_hint)
    if not core_terms:
        return queries

    anchored: list[str] = []
    for q in queries:
        q_lower = q.lower()
        missing = [t for t in core_terms if t.lower() not in q_lower]
        if missing:
            q_lower_words = set(q_lower.split())
            stop = {"the", "a", "an", "is", "are", "of", "for", "and", "or", "to", "in", "on", "with", "how", "what", "why"}
            useful_words = q_lower_words - stop
            if not useful_words or _is_offtopic_query(q, entity_hint):
                anchored.append(f"{entity_hint} {' '.join(w for w in q.split() if w.lower() not in stop)[:60]}")
            else:
                anchored.append(f"{' '.join(missing)} {q}"[:120])
        else:
            anchored.append(q)

    return list(dict.fromkeys(anchored))


def _get_core_terms(entity_hint: str) -> list[str]:
    """Extract must-appear terms from the entity hint."""
    hint_lower = entity_hint.lower()
    if "amazing spider-man" in hint_lower:
        return ["Amazing Spider-Man"]
    if "spider-man" in hint_lower:
        return ["Spider-Man"]
    words = [w for w in entity_hint.split() if len(w) > 2 and w.lower() not in ("the", "comics", "dc", "marvel")]
    return words[:2] if words else []


_OFFTOPIC_PATTERNS = re.compile(
    r"^(what\s+(is|does|means?)|define |meaning |definition |"
    r"how\s+to\s+say|translate |synonym|antonym|"
    r"english\s+(meaning|definition|word))",
    re.IGNORECASE,
)


def _is_offtopic_query(query: str, entity_hint: str) -> bool:
    """Detect queries that are clearly about word definitions, not the entity."""
    if _OFFTOPIC_PATTERNS.search(query):
        return True
    hint_words = set(entity_hint.lower().split())
    query_words = set(query.lower().split())
    overlap = hint_words & query_words
    if len(overlap) <= 1 and len(query_words) > 2:
        return True
    return False


def _parse_plan_json(raw: str, config: dict) -> ResearchPlan | None:
    try:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None
        data = json.loads(match.group())

        queries = data.get("queries", [])
        if not isinstance(queries, list) or not queries:
            return None
        queries = [q for q in queries if isinstance(q, str) and len(q.strip()) > 3]
        queries = queries[:config["max_queries"]]

        subquestions = data.get("subquestions", [])
        if not isinstance(subquestions, list):
            subquestions = []
        subquestions = [s for s in subquestions if isinstance(s, str) and len(s.strip()) > 5]

        return ResearchPlan(
            objective=str(data.get("objective", "")),
            subquestions=subquestions[:8],
            queries=queries,
            freshness_required=bool(data.get("freshness_required", False)),
            source_preferences=[str(s) for s in data.get("source_preferences", []) if isinstance(s, str)],
            expected_output=str(data.get("expected_output", "overview")),
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _parse_json_array(raw: str) -> list[str]:
    try:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return []
        data = json.loads(match.group())
        if isinstance(data, list):
            return [str(item) for item in data if isinstance(item, str) and len(item.strip()) > 3]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _fallback_plan(query: str, config: dict, entity_hint: str = "") -> ResearchPlan:
    words = query.lower().split()
    is_comparison = any(w in words for w in ("vs", "versus", "compare", "comparison", "or"))
    anchor = entity_hint or query

    base_queries = [anchor]

    if is_comparison:
        parts = re.split(r"\bvs\.?\b|\bversus\b|\bor\b|\bcompare\b", query, flags=re.IGNORECASE)
        for part in parts:
            cleaned = part.strip().strip(".,!?")
            if cleaned and len(cleaned) > 3:
                base_queries.append(f"{cleaned} features pros cons")
                base_queries.append(f"{cleaned} review 2024 2025")
    else:
        base_queries.append(f"{anchor} overview")
        base_queries.append(f"{anchor} history")
        base_queries.append(f"{anchor} official site")
        base_queries.append(f"{anchor} Wikipedia")
        if entity_hint:
            base_queries.append(f"{query} publication history")
            base_queries.append(f"{query} major storylines")

    base_queries.append(f"{anchor} Reddit")
    base_queries.append(f"{anchor} guide")

    queries = list(dict.fromkeys(base_queries))[:config["max_queries"]]

    subquestions = [f"What is {anchor}?"]
    if is_comparison:
        subquestions.append("What are the key differences?")
        subquestions.append("What are the tradeoffs?")
        subquestions.append("Which is better for the user's specific case?")
    else:
        subquestions.append(f"What is the history of {anchor}?")
        subquestions.append(f"What are the key facts about {anchor}?")
        subquestions.append(f"What do experts say about {anchor}?")

    return ResearchPlan(
        objective=f"Research: {anchor}",
        subquestions=subquestions,
        queries=queries,
        freshness_required=True,
        source_preferences=["official sources", "Wikipedia", "dedicated databases", "expert articles"],
        expected_output="comparison" if is_comparison else "overview",
    )
