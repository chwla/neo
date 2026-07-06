"""Topic intent classification for ambiguous short Research Mode queries."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOPIC_AI_CODING_TOOLS = "ai_coding_tools_comparison"

# Longer keys first for greedy matching in query text.
AI_CODING_TOOL_ALIASES: list[tuple[str, str]] = [
    ("claude code", "Anthropic Claude Code"),
    ("codex cli", "OpenAI Codex CLI"),
    ("cursor pro", "Cursor AI Pro plan"),
    ("codex pro", "OpenAI Codex Pro plan"),
    ("github copilot", "GitHub Copilot"),
    ("copilot", "GitHub Copilot"),
    ("windsurf", "Windsurf AI editor"),
    ("cursor", "Cursor AI code editor / AI IDE"),
    ("codex", "OpenAI Codex / Codex CLI / cloud coding agent"),
    ("claude", "Anthropic Claude Code"),
]

_COMPARISON_SIGNAL = re.compile(
    r"\b(vs\.?|versus|compare|comparison|compared\s+to|or\s+\w+\s+pro\b|\bor\b)\b",
    re.IGNORECASE,
)

_PRICING_SIGNAL = re.compile(r"\b(pro|plus|team|pricing|price|plan|subscription)\b", re.IGNORECASE)

_OFFTOPIC_QUERY_PATTERNS = re.compile(
    r"\b("
    r"sql\s+cursor|database\s+cursor|server[- ]side\s+cursor|"
    r"what\s+is\s+a?\s*cursor|cursor\s+definition|cursor\s+meaning|"
    r"ui\s+cursor|mouse\s+cursor|cursor\s+pointer|blinking\s+cursor|"
    r"ancient\s+codex|codex\s+manuscript|medieval\s+codex|"
    r"codex\s+definition|codex\s+meaning|historical\s+origins?|"
    r"data\s+storage|retrieval|literature|philosophy|"
    r"geeksforgeeks\s+cursor"
    r")\b",
    re.IGNORECASE,
)

_OFFTOPIC_SOURCE_TITLE = re.compile(
    r"\b("
    r"sql\s+cursor|database\s+cursor|what\s+is\s+cursor|cursor\s+in\s+python|"
    r"mouse\s+cursor|ui\s+cursor|pointer\s+cursor|"
    r"ancient\s+codex|medieval\s+codex|manuscript\s+codex|"
    r"codex\s+definition|meaning\s+of\s+codex"
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

_CURSOR_EVIDENCE_TERMS = frozenset(
    {
        "cursor ai",
        "cursor.com",
        "cursor ide",
        "cursor editor",
        "ai editor",
        "ai ide",
        "codebase indexing",
        "codebase index",
        "composer",
        "cursor agent",
        "vs code fork",
        "vscode fork",
        "cursor pro",
        "anysphere",
    }
)

_CODEX_EVIDENCE_TERMS = frozenset(
    {
        "openai codex",
        "codex cli",
        "coding agent",
        "cloud coding agent",
        "openai coding agent",
        "chatgpt codex",
        "openai/codex",
        "github.com/openai/codex",
        "codex pro",
        "openai.com/codex",
    }
)

_CLAUDE_EVIDENCE_TERMS = frozenset(
    {
        "claude code",
        "anthropic claude code",
        "anthropic.com",
    }
)

_COPILOT_EVIDENCE_TERMS = frozenset(
    {
        "github copilot",
        "copilot",
        "docs.github.com/copilot",
    }
)

_WINDSURF_EVIDENCE_TERMS = frozenset(
    {
        "windsurf",
        "codeium windsurf",
        "windsurf editor",
    }
)

_REJECT_EVIDENCE_TERMS = frozenset(
    {
        "sql cursor",
        "database cursor",
        "server-side cursor",
        "fetch cursor",
        "mouse cursor",
        "ui cursor",
        "cursor pointer",
        "blinking cursor",
        "text cursor",
        "screen cursor",
        "ancient codex",
        "medieval codex",
        "manuscript codex",
        "codex book",
        "historical codex",
        "codex as book",
    }
)

_TOOL_EVIDENCE_MAP: dict[str, frozenset[str]] = {
    "cursor": _CURSOR_EVIDENCE_TERMS,
    "codex": _CODEX_EVIDENCE_TERMS,
    "claude code": _CLAUDE_EVIDENCE_TERMS,
    "claude": _CLAUDE_EVIDENCE_TERMS,
    "copilot": _COPILOT_EVIDENCE_TERMS,
    "github copilot": _COPILOT_EVIDENCE_TERMS,
    "windsurf": _WINDSURF_EVIDENCE_TERMS,
}


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

    tools = _detect_ai_coding_tools(q)
    if not tools:
        return None

    is_comparison = bool(_COMPARISON_SIGNAL.search(q)) or len(tools) >= 2
    if not is_comparison and len(tools) == 1:
        # Single tool mention without comparison — not this intent path.
        if not _PRICING_SIGNAL.search(q):
            return None

    normalized = {}
    for slug in tools:
        for alias, label in AI_CODING_TOOL_ALIASES:
            if alias == slug or slug.startswith(alias.split()[0]):
                normalized[slug] = label
                break
        if slug not in normalized:
            normalized[slug] = slug

    return TopicIntent(
        topic_intent=TOPIC_AI_CODING_TOOLS,
        tools=tools,
        normalized_entities=normalized,
        pricing_focus=bool(_PRICING_SIGNAL.search(q)),
        comparison_query=is_comparison or len(tools) >= 2,
        original_query=orig,
    )


def _detect_ai_coding_tools(query: str) -> list[str]:
    q = query.lower()
    found: list[str] = []
    used_spans: list[tuple[int, int]] = []

    for alias, _ in sorted(AI_CODING_TOOL_ALIASES, key=lambda x: len(x[0]), reverse=True):
        for match in re.finditer(re.escape(alias), q):
            start, end = match.span()
            if any(start < u_end and end > u_start for u_start, u_end in used_spans):
                continue
            slug = (
                alias.split()[0]
                if alias
                in ("claude code", "codex cli", "cursor pro", "codex pro", "github copilot")
                else alias
            )
            if alias == "claude code":
                slug = "claude code"
            elif alias == "codex cli":
                slug = "codex"
            elif alias == "cursor pro":
                slug = "cursor"
            elif alias == "codex pro":
                slug = "codex"
            elif alias == "github copilot":
                slug = "copilot"
            if slug not in found:
                found.append(slug)
            used_spans.append((start, end))

    # cursor vs codex — require at least cursor or codex in coding-tool sense
    coding_slugs = {"cursor", "codex", "claude code", "claude", "copilot", "windsurf"}
    return [t for t in found if t in coding_slugs]


def is_offtopic_ai_coding_query(query: str) -> bool:
    return bool(_OFFTOPIC_QUERY_PATTERNS.search(query))


def filter_offtopic_ai_coding_queries(queries: list[str]) -> list[str]:
    return [q for q in queries if not is_offtopic_ai_coding_query(q)]


def build_ai_coding_plan(intent: TopicIntent, user_query: str) -> dict:
    """Deterministic plan payload for AI coding tool comparisons."""
    tools = intent.tools
    entities = list(intent.normalized_entities.values())
    entity_a = intent.normalized_entities.get(tools[0], tools[0]) if tools else "Cursor AI"
    entity_b = (
        intent.normalized_entities.get(tools[1], tools[1]) if len(tools) > 1 else "OpenAI Codex"
    )

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

    queries = [
        "Cursor AI editor official pricing features",
        "OpenAI Codex official coding agent pricing features",
        "OpenAI Codex CLI GitHub",
        "Cursor AI vs OpenAI Codex comparison",
        "Cursor AI documentation codebase indexing",
        "OpenAI Codex documentation cloud coding agent",
        "Cursor pricing Pro plan",
        "OpenAI Codex availability ChatGPT Plus Pro Team",
        "cursor.com docs agent composer",
        "openai.com codex coding agent",
    ]

    if "claude" in tools or "claude code" in tools:
        queries.extend(
            [
                "Anthropic Claude Code official documentation pricing",
                "Claude Code vs Cursor AI comparison",
            ]
        )
    if "copilot" in tools:
        queries.extend(
            [
                "GitHub Copilot official pricing features documentation",
                "GitHub Copilot vs Cursor AI comparison",
            ]
        )
    if "windsurf" in tools:
        queries.extend(
            [
                "Windsurf AI editor official pricing features",
                "Windsurf vs Cursor AI comparison",
            ]
        )

    if intent.pricing_focus:
        queries = [
            "Cursor AI Pro plan pricing features",
            "OpenAI Codex Pro Plus Team pricing",
            "Cursor vs OpenAI Codex pricing comparison",
        ] + queries

    objective = (
        f"Compare {' vs '.join(entities)} as AI coding tools for software development — "
        "not generic dictionary, historical, SQL/UI cursor, or manuscript meanings."
    )

    return {
        "objective": objective,
        "subquestions": subquestions[:8],
        "queries": list(dict.fromkeys(queries)),
        "freshness_required": True,
        "source_preferences": [
            "cursor.com official docs/pricing/blog",
            "openai.com Codex docs/blog/help",
            "github.com/openai/codex",
            "official changelogs",
            "reputable developer tooling blogs",
        ],
        "expected_output": "comparison",
    }


def ai_coding_entity_terms(intent: TopicIntent) -> list[str]:
    terms: set[str] = set()
    for slug in intent.tools:
        terms.update(_TOOL_EVIDENCE_MAP.get(slug, set()))
    terms.update({"ai coding", "coding agent", "ai editor", "code editor"})
    return sorted(terms)


def classify_evidence_category(text: str, source: "ResearchSource", intent: TopicIntent) -> str:
    """Tag evidence as cursor/codex/comparison/irrelevant for AI coding comparisons."""
    from app.services.research.types import ResearchSource  # noqa: F401 — type hint only

    combined = f"{source.title} {source.url} {text}".lower()

    for reject in _REJECT_EVIDENCE_TERMS:
        if reject in combined:
            return "irrelevant"

    if _OFFTOPIC_SOURCE_TITLE.search(source.title) and not _mentions_any_tool(combined, intent):
        return "irrelevant"

    tool_hits: dict[str, bool] = {}
    for slug in intent.tools:
        terms = _TOOL_EVIDENCE_MAP.get(slug, frozenset())
        tool_hits[slug] = any(t in combined for t in terms) or slug in combined

    hit_count = sum(1 for v in tool_hits.values() if v)
    if hit_count >= 2:
        return "comparison_evidence"
    if tool_hits.get("cursor"):
        return "cursor_evidence"
    if tool_hits.get("codex"):
        return "codex_evidence"
    if tool_hits.get("claude code") or tool_hits.get("claude"):
        return "claude_evidence"
    if tool_hits.get("copilot"):
        return "copilot_evidence"
    if tool_hits.get("windsurf"):
        return "windsurf_evidence"

    if _mentions_any_tool(combined, intent):
        return "general"

    return "irrelevant"


def _mentions_any_tool(text: str, intent: TopicIntent) -> bool:
    for slug in intent.tools:
        if slug in text:
            return True
        for term in _TOOL_EVIDENCE_MAP.get(slug, ()):
            if term in text:
                return True
    return False


def source_is_offtopic_for_ai_coding(source: "ResearchSource", intent: TopicIntent) -> str | None:
    """Return rejection reason if source should be rejected."""
    from app.services.research.types import ResearchSource  # noqa: F401

    title = (source.title or "").lower()
    url = (source.url or "").lower()
    domain = (source.domain or "").lower()
    text_sample = (source.text or "")[:4000].lower()
    combined = f"{title} {url} {text_sample}"

    if _OFFTOPIC_SOURCE_TITLE.search(title):
        if not _mentions_any_tool(combined, intent):
            return "Irrelevant title (generic cursor/codex meaning)"

    for reject in _REJECT_EVIDENCE_TERMS:
        if reject in combined and not _mentions_any_tool(combined, intent):
            return f"Irrelevant content ({reject})"

    if "geeksforgeeks" in domain and "cursor" in combined and "sql" in combined:
        return "Irrelevant domain (SQL cursor tutorial)"

    if "wikipedia.org" in domain:
        if (
            re.search(r"\bcursor\b", title)
            and "computing" in combined[:500]
            and "ai" not in combined[:800]
        ):
            if "cursor.com" not in combined and "cursor ai" not in combined:
                return "Irrelevant Wikipedia (UI/computing cursor)"

    return None


def is_preferred_ai_coding_source(source: "ResearchSource") -> bool:
    domain = (source.domain or "").lower()
    url = (source.url or "").lower()
    if "cursor.com" in domain:
        return True
    if "openai.com" in domain and "codex" in url:
        return True
    if "github.com" in domain and "openai/codex" in url:
        return True
    if "docs.github.com" in domain and "copilot" in url:
        return True
    if "anthropic.com" in domain:
        return True
    return False


def is_low_quality_ai_coding_source(source: "ResearchSource") -> bool:
    domain = (source.domain or "").lower()
    return any(d in domain for d in _LOW_QUALITY_DOMAINS)


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
