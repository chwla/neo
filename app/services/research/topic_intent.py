"""Topic intent classification for ambiguous short Research Mode queries.

The coding-tool path is deliberately vendor-neutral: entities come from the user's
question, not from a hard-coded comparison pair. A small registry of well-known tool
names helps recognition and gives nicer labels, but any tool named in a comparison is
supported, and every downstream step (query generation, evidence categorisation,
coverage scoring, source preference) works off the detected entities.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.research.types import ResearchSource

TOPIC_AI_CODING_TOOLS = "ai_coding_tools_comparison"

# Recognition hints only — NOT a comparison pair. Longer keys first for greedy matching.
# Any tool named in the question is supported whether or not it appears here; this table
# exists to give known names a tidy label and to confirm a query is about coding tools.
KNOWN_CODING_TOOLS: list[tuple[str, str]] = [
    ("github copilot", "GitHub Copilot"),
    ("sourcegraph cody", "Sourcegraph Cody"),
    ("jetbrains ai", "JetBrains AI Assistant"),
    ("amazon q developer", "Amazon Q Developer"),
    ("continue.dev", "Continue"),
    ("copilot", "GitHub Copilot"),
    ("windsurf", "Windsurf"),
    ("codeium", "Codeium"),
    ("tabnine", "Tabnine"),
    ("cursor", "Cursor"),
    ("aider", "Aider"),
    ("cody", "Sourcegraph Cody"),
]

_COMPARISON_SIGNAL = re.compile(
    r"\b(vs\.?|versus|compare|comparison|compared\s+to|or\s+\w+\s+pro\b|\bor\b)\b",
    re.IGNORECASE,
)

_PRICING_SIGNAL = re.compile(r"\b(pro|plus|team|pricing|price|plan|subscription)\b", re.IGNORECASE)

# Signals that a query is about software/coding tooling. Lets an unknown tool name be
# accepted as a comparison entity without hard-coding the vendor.
_CODING_CONTEXT_SIGNAL = re.compile(
    r"\b("
    r"ai\s+(?:coding|code|dev|developer|programming)|"
    r"coding\s+(?:agent|assistant|tool|tools|copilot|cli)|"
    r"code\s+(?:assistant|editor|completion|generation|review|search)|"
    r"pair\s+programm\w*|autocomplete|code\s+intelligence|"
    r"\bide\b|code\s+editor|text\s+editor|"
    r"dev(?:eloper)?\s+tool(?:s|ing)?|programming\s+(?:assistant|tool|tools)|"
    r"software\s+development|software\s+engineer\w*|"
    r"\bcoding\b|\bprogramming\b|\brefactor\w*|\bcodebase\b|\brepo(?:sitory)?\b"
    r")\b",
    re.IGNORECASE,
)

# Splits "X vs Y" style questions into their two sides.
_COMPARISON_SPLIT = re.compile(
    r"\b(?:vs\.?|versus|compared\s+to|comparison\s+of|compare|or)\b",
    re.IGNORECASE,
)

_DIFFERENCE_BETWEEN = re.compile(
    r"\bdifference\s+between\s+(.+?)\s+and\s+(.+)$",
    re.IGNORECASE,
)

# Trailing use-case qualifiers to strip off an entity ("cursor for python" -> "cursor").
_QUALIFIER_SPLIT = re.compile(
    r"\b(for|in|as|on|with|under|during|from|when)\b",
    re.IGNORECASE,
)

_LEADING_FILLER = re.compile(
    r"^(?:what(?:'s| is| are)?|which|who|how|is|are|should\s+i\s+use|"
    r"tell\s+me\s+about|the|a|an|best|better)\s+",
    re.IGNORECASE,
)

# Word senses that mean the query/source is not about software at all. Keyed by tool
# name because ambiguity is a property of the word, not of any particular vendor.
_AMBIGUOUS_TOOL_SENSES: dict[str, re.Pattern[str]] = {
    "cursor": re.compile(
        r"\b("
        r"sql\s+cursors?|database\s+cursors?|server[- ]side\s+cursors?|"
        r"fetch\s+cursor|cursor\s+in\s+(?:python|sql|java|c\+\+)|"
        r"mouse\s+cursor|ui\s+cursor|cursor\s+pointer|pointer\s+cursor|"
        r"blinking\s+cursor|text\s+cursor|screen\s+cursor|cursor\s+in\s+computing"
        r")\b",
        re.IGNORECASE,
    ),
}

# Non-software senses that apply to any tool name (dictionary/etymology pages).
_GENERIC_NON_SOFTWARE_SENSE = re.compile(
    r"\b("
    r"\w+\s+definition|definition\s+of\s+\w+|\w+\s+meaning|meaning\s+of\s+\w+|"
    r"etymology|historical\s+origins?|dictionary|thesaurus|synonyms?\s+of"
    r")\b",
    re.IGNORECASE,
)

_LOW_QUALITY_DOMAINS = frozenset(
    {
        "linkedin.com",
        "medium.com",
        "quora.com",
        "pinterest.com",
        "finance.yahoo.com",
        "podcasts.apple.com",
        "spotify.com",
    }
)

# Generic vocabulary shared by every coding-tool comparison, regardless of vendor.
_GENERIC_CODING_TERMS = frozenset(
    {
        "ai coding",
        "coding agent",
        "coding assistant",
        "ai editor",
        "code editor",
        "code completion",
        "developer tool",
        "ide",
    }
)

# Generic markers of an official/first-party documentation source.
_OFFICIAL_SUBDOMAIN_HINTS = ("docs.", "developer.", "developers.", "help.", "support.")
_DEVELOPER_HOSTING_DOMAINS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

# Positional labels so evidence, coverage and scoring stay entity-agnostic.
_ENTITY_LETTERS = ("a", "b", "c", "d", "e", "f")

COMPARISON_TABLE_DIMENSIONS = [
    "Product type",
    "Best use case",
    "Workflow",
    "Strengths",
    "Weaknesses",
    "Pricing / plan model",
    "Local vs cloud behavior",
    "Codebase context/indexing",
    "Agent autonomy",
    "Privacy/control",
    "Recommended user",
]


@dataclass
class TopicIntent:
    topic_intent: str
    tools: list[str]
    normalized_entities: dict[str, str] = field(default_factory=dict)
    pricing_focus: bool = False
    comparison_query: bool = True
    original_query: str = ""
    normalized_query: str | None = None
    normalization_reason: str | None = None
    ai_workload_focus: bool = False
    product_pair: str | None = None


def entity_evidence_category(index: int) -> str:
    """Positional evidence category for the *index*-th comparison entity."""

    if 0 <= index < len(_ENTITY_LETTERS):
        return f"entity_{_ENTITY_LETTERS[index]}_evidence"
    return "general"


def entity_index_for_category(category: str) -> int | None:
    """Inverse of :func:`entity_evidence_category`."""

    match = re.fullmatch(r"entity_([a-f])_evidence", category or "")
    if not match:
        return None
    return _ENTITY_LETTERS.index(match.group(1))


def classify_topic_intent(user_query: str, original_query: str | None = None) -> TopicIntent | None:
    """Classify short ambiguous queries into Neo-relevant topic intents."""
    from app.services.research.product_intent import classify_product_intent

    orig = (original_query or user_query).strip()
    q = user_query.strip()
    if not q:
        return None

    product = classify_product_intent(q, original_query=orig)
    if product:
        return TopicIntent(
            topic_intent=product.topic_intent,
            tools=product.entities,
            normalized_entities=product.normalized_entities,
            pricing_focus=product.pricing_focus,
            comparison_query=product.comparison_query,
            original_query=product.original_query,
            normalized_query=product.normalized_query,
            normalization_reason=product.normalization_reason,
            ai_workload_focus=product.ai_workload_focus,
            product_pair=product.product_pair,
        )

    detected = detect_coding_tools(q)
    if not detected:
        return None

    tools = [slug for slug, _ in detected]
    normalized = {slug: label for slug, label in detected}

    is_comparison = bool(_COMPARISON_SIGNAL.search(q)) or len(tools) >= 2
    if not is_comparison and len(tools) == 1:
        # Single tool mention without comparison — not this intent path.
        if not _PRICING_SIGNAL.search(q):
            return None

    return TopicIntent(
        topic_intent=TOPIC_AI_CODING_TOOLS,
        tools=tools,
        normalized_entities=normalized,
        pricing_focus=bool(_PRICING_SIGNAL.search(q)),
        comparison_query=is_comparison or len(tools) >= 2,
        original_query=orig,
    )


def detect_coding_tools(query: str) -> list[tuple[str, str]]:
    """Detect the coding tools a question is about, as ``(slug, label)`` pairs.

    Known names are matched directly; any other entity named on either side of a
    comparison is accepted when the question is recognisably about software tooling.
    """

    q = query.strip()
    if not q:
        return []

    known = _match_known_tools(q)
    detected: list[tuple[str, str]] = list(known)

    for side in _comparison_sides(q):
        slug = _entity_slug(side)
        if not slug:
            continue
        if any(
            slug == existing or slug in existing or existing in slug
            for existing, _ in detected
        ):
            continue
        detected.append((slug, _entity_label(side)))

    if not detected:
        return []

    # Only claim the coding-tool topic when the question is plausibly about tooling:
    # a recognised tool, or an explicit software/coding context.
    if not known and not _CODING_CONTEXT_SIGNAL.search(q):
        return []

    return detected


def _match_known_tools(query: str) -> list[tuple[str, str]]:
    """Known tools in the order they appear in the question.

    Longest aliases are matched first so "github copilot" wins over "copilot", but the
    result is ordered by position: entity A is whichever tool the user named first.
    """

    q = query.lower()
    found: list[tuple[int, str, str]] = []
    used_spans: list[tuple[int, int]] = []

    for alias, label in sorted(KNOWN_CODING_TOOLS, key=lambda x: len(x[0]), reverse=True):
        for match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", q):
            start, end = match.span()
            if any(start < u_end and end > u_start for u_start, u_end in used_spans):
                continue
            if not any(existing == alias for _, existing, _ in found):
                found.append((start, alias, label))
            used_spans.append((start, end))

    return [(slug, label) for _, slug, label in sorted(found, key=lambda item: item[0])]


def _comparison_sides(query: str) -> list[str]:
    """Split a comparison question into its entity sides, without topic knowledge."""

    q = re.sub(r"\s+", " ", query.strip())

    diff = _DIFFERENCE_BETWEEN.search(q)
    if diff:
        return [diff.group(1).strip(" ?.,:"), diff.group(2).strip(" ?.,:")]

    if not _COMPARISON_SIGNAL.search(q):
        return []

    parts = [part.strip(" ?.,:") for part in _COMPARISON_SPLIT.split(q)]
    return [part for part in parts if part]


def _clean_entity(value: str) -> str:
    value = _LEADING_FILLER.sub("", value.strip())
    value = re.sub(r"\s+", " ", value)
    match = _QUALIFIER_SPLIT.search(value)
    if match and match.start() > 0:
        value = value[: match.start()]
    # Drop a trailing topic descriptor ("Foo ai coding agent" -> "Foo") so the entity is
    # the tool name alone.
    context = _CODING_CONTEXT_SIGNAL.search(value)
    if context and context.start() > 0:
        value = value[: context.start()]
    return value.strip(" ?.,:'\"")


def _entity_slug(value: str) -> str:
    cleaned = _clean_entity(value).lower()
    if not cleaned:
        return ""
    words = cleaned.split()
    # Entity names are short; anything longer is prose, not a tool name.
    if not words or len(words) > 4:
        return ""
    if not re.search(r"[a-z0-9]", cleaned):
        return ""
    return cleaned


def _entity_label(value: str) -> str:
    cleaned = _clean_entity(value)
    slug = cleaned.lower()
    for alias, label in KNOWN_CODING_TOOLS:
        if alias == slug:
            return label
    return " ".join(word if word.isupper() else word.capitalize() for word in cleaned.split())


def is_offtopic_ai_coding_query(query: str) -> bool:
    """True when a query asks about a non-software sense of a tool name."""

    text = query or ""
    for pattern in _AMBIGUOUS_TOOL_SENSES.values():
        if pattern.search(text):
            return True
    return bool(_GENERIC_NON_SOFTWARE_SENSE.search(text))


def filter_offtopic_ai_coding_queries(queries: list[str]) -> list[str]:
    return [q for q in queries if not is_offtopic_ai_coding_query(q)]


def build_ai_coding_plan(intent: TopicIntent, user_query: str) -> dict:
    """Deterministic plan payload built from the entities detected in the question."""
    entities = _entity_labels(intent)
    entity_a = entities[0] if entities else "the first tool"
    entity_b = entities[1] if len(entities) > 1 else "the alternative tool"

    subquestions = [
        f"What is {entity_a} and what is it best for?",
        f"What is {entity_b} and what is it best for?",
        f"How do {entity_a} and {entity_b} compare on workflow, pricing, and agent capabilities?",
        "Which tool is better for local vs cloud coding workflows?",
        "What are the main tradeoffs for a developer choosing between them?",
    ]
    if intent.pricing_focus:
        subquestions.insert(
            2, f"How do {entity_a} and {entity_b} pricing plans compare (Pro/Plus/Team)?"
        )

    queries: list[str] = []
    for entity in entities:
        queries.extend(
            [
                f"{entity} official documentation features",
                f"{entity} official pricing plans",
            ]
        )

    # Pairwise comparison queries across every detected entity.
    for i, first in enumerate(entities):
        for second in entities[i + 1 :]:
            queries.append(f"{first} vs {second} comparison")
            queries.append(f"{first} vs {second} developer review")

    for entity in entities:
        queries.append(f"{entity} coding agent codebase context capabilities")

    if intent.pricing_focus:
        pricing_first = [f"{entity} pricing plan cost" for entity in entities]
        if len(entities) > 1:
            pricing_first.append(f"{entities[0]} vs {entities[1]} pricing comparison")
        queries = pricing_first + queries

    if not queries:
        queries = [f"{user_query} official documentation"]

    joined = " vs ".join(entities) if entities else user_query
    objective = (
        f"Compare {joined} as software development / AI coding tools — "
        "focus on the software products, not unrelated meanings of their names."
    )

    source_preferences = [f"{entity} official docs/pricing/blog" for entity in entities]
    source_preferences += [
        "official changelogs and release notes",
        "reputable developer tooling blogs",
    ]

    return {
        "objective": objective,
        "subquestions": subquestions[:8],
        "queries": list(dict.fromkeys(queries)),
        "freshness_required": True,
        "source_preferences": source_preferences,
        "expected_output": "comparison",
    }


def _entity_labels(intent: TopicIntent) -> list[str]:
    labels: list[str] = []
    for slug in intent.tools:
        label = intent.normalized_entities.get(slug, slug)
        if label and label not in labels:
            labels.append(label)
    return labels


def _entity_terms(slug: str, label: str) -> set[str]:
    """Search/match terms for one entity, derived from its own name."""

    terms = {slug.lower(), label.lower()}
    # A multi-word name is also worth matching without its separators (e.g. domains).
    compact = re.sub(r"[^a-z0-9]", "", label.lower())
    if len(compact) >= 4:
        terms.add(compact)
    return {term for term in terms if len(term) >= 3}


def ai_coding_entity_terms(intent: TopicIntent) -> list[str]:
    terms: set[str] = set()
    for slug in intent.tools:
        terms.update(_entity_terms(slug, intent.normalized_entities.get(slug, slug)))
    terms.update(_GENERIC_CODING_TERMS)
    return sorted(terms)


def _entity_domain_tokens(slug: str, label: str) -> set[str]:
    """Tokens that would plausibly appear in the entity's own official domain."""

    tokens: set[str] = set()
    for source in (slug, label):
        compact = re.sub(r"[^a-z0-9]", "", source.lower())
        if len(compact) >= 4:
            tokens.add(compact)
        for word in re.split(r"[^a-z0-9]+", source.lower()):
            if len(word) >= 4:
                tokens.add(word)
    return tokens


def _mentions_entity(text: str, slug: str, label: str) -> bool:
    return any(term in text for term in _entity_terms(slug, label))


def _mentions_any_tool(text: str, intent: TopicIntent) -> bool:
    return any(
        _mentions_entity(text, slug, intent.normalized_entities.get(slug, slug))
        for slug in intent.tools
    )


def _mentions_entity_strongly(text: str, slug: str, label: str) -> bool:
    """A mention that survives an ambiguous name.

    When a tool's name is also an everyday word, the bare word proves nothing — SQL
    cursors and mouse cursors both "mention cursor". A mention only counts if the tool's
    own domain appears, or the name sits in an explicit software/coding context.
    """

    if not _mentions_entity(text, slug, label):
        return False
    if any(
        f"{token}." in text or f"/{token}" in text
        for token in _entity_domain_tokens(slug, label)
    ):
        return True
    return bool(_CODING_CONTEXT_SIGNAL.search(text))


def _mentions_any_tool_strongly(text: str, intent: TopicIntent) -> bool:
    return any(
        _mentions_entity_strongly(text, slug, intent.normalized_entities.get(slug, slug))
        for slug in intent.tools
    )


def _non_software_sense(text: str, intent: TopicIntent) -> bool:
    """True when text uses a tool name in a clearly non-software sense."""

    for slug in intent.tools:
        pattern = _AMBIGUOUS_TOOL_SENSES.get(slug)
        if pattern and pattern.search(text):
            return True
    return False


def classify_evidence_category(text: str, source: ResearchSource, intent: TopicIntent) -> str:
    """Tag evidence against the detected entities, positionally (entity A/B/...)."""
    combined = f"{source.title} {source.url} {text}".lower()

    if _non_software_sense(combined, intent) and not _mentions_any_tool_strongly(combined, intent):
        return "irrelevant"

    if _GENERIC_NON_SOFTWARE_SENSE.search(combined) and not _mentions_any_tool_strongly(
        combined, intent
    ):
        return "irrelevant"

    hit_indexes = [
        index
        for index, slug in enumerate(intent.tools)
        if _mentions_entity(combined, slug, intent.normalized_entities.get(slug, slug))
    ]

    if len(hit_indexes) >= 2:
        return "comparison_evidence"
    if hit_indexes:
        return entity_evidence_category(hit_indexes[0])

    return "irrelevant"


def source_is_offtopic_for_ai_coding(source: ResearchSource, intent: TopicIntent) -> str | None:
    """Return rejection reason if source should be rejected."""
    title = (source.title or "").lower()
    url = (source.url or "").lower()
    domain = (source.domain or "").lower()
    text_sample = (source.text or "")[:4000].lower()
    combined = f"{title} {url} {text_sample}"

    if _non_software_sense(combined, intent) and not _mentions_any_tool_strongly(combined, intent):
        return "Irrelevant content (non-software sense of the tool name)"

    if _GENERIC_NON_SOFTWARE_SENSE.search(title) and not _mentions_any_tool_strongly(
        combined, intent
    ):
        return "Irrelevant title (dictionary/definition sense)"

    if "wikipedia.org" in domain and not _mentions_any_tool_strongly(combined, intent):
        if _non_software_sense(combined, intent):
            return "Irrelevant Wikipedia (non-software sense)"

    return None


def is_official_source_for_entity(source: ResearchSource, slug: str, label: str) -> bool:
    """True when a source looks first-party for one entity, judged by its own name.

    Derived from the entity name rather than a vendor→domain table, so an unknown tool
    gets the same treatment as a well-known one.
    """

    domain = (source.domain or "").lower()
    url = (source.url or "").lower()
    for token in _entity_domain_tokens(slug, label):
        if token in domain:
            return True
        # A project page on a developer host counts as first-party too.
        if token in url and any(host in domain for host in _DEVELOPER_HOSTING_DOMAINS):
            return True
    return False


def is_preferred_ai_coding_source(
    source: ResearchSource, intent: TopicIntent | None = None
) -> bool:
    """Prefer a tool's own site/docs, derived from the entity name — no vendor table."""
    if intent and any(
        is_official_source_for_entity(source, slug, intent.normalized_entities.get(slug, slug))
        for slug in intent.tools
    ):
        return True

    return (source.domain or "").lower().startswith(_OFFICIAL_SUBDOMAIN_HINTS)


def is_low_quality_ai_coding_source(source: ResearchSource) -> bool:
    domain = (source.domain or "").lower()
    return any(d in domain for d in _LOW_QUALITY_DOMAINS)
