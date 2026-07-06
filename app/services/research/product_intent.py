"""Product comparison intent, query normalization, and entity-locked planning."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TOPIC_PRODUCT_COMPARISON = "product_comparison"

_COMPARISON_SIGNAL = re.compile(
    r"\b(vs\.?|versus|compare|comparison|compared\s+to|\bor\b)\b",
    re.IGNORECASE,
)

_PRICING_SIGNAL = re.compile(
    r"\b(pricing|price|cost|how much|india|inr|₹|\$)\b",
    re.IGNORECASE,
)

# Explicit AI-workload queries — do NOT normalize "ai" → "air"
_AI_WORKLOAD_EXPLICIT = re.compile(
    r"\b("
    r"best\s+macbook\s+for\s+ai|macbook\s+for\s+local\s+ai|macbook\s+for\s+ai|"
    r"macbook\s+pro\s+for\s+ai|macbook\s+air\s+for\s+ai|"
    r"local\s+ai|ai\s+workload|ai\s+ml|machine\s+learning|llm\s+on\s+mac|"
    r"ai\s+pc|copilot\+?\s*pc"
    r")\b",
    re.IGNORECASE,
)

_MACBOOK_AI_TYPO = re.compile(
    r"macbook\s+ai\s+(vs\.?|versus|or|compared\s+to)\s+macbook\s+pro",
    re.IGNORECASE,
)
_MACBOOK_PRO_AI_TYPO = re.compile(
    r"macbook\s+pro\s+(vs\.?|versus|or|compared\s+to)\s+macbook\s+ai\b",
    re.IGNORECASE,
)

_OFFTOPIC_QUERY_PATTERNS = re.compile(
    r"\b("
    r"macbook\s+neo|"
    r"windows\s+ai\s+pc|ai\s+laptop\s+vs\s+macbook|"
    r"rumor|leak|foldable\s+macbook"
    r")\b",
    re.IGNORECASE,
)

_OFFTOPIC_SOURCE_PATTERNS = re.compile(
    r"\b("
    r"macbook\s+neo|best\s+macbook\s+for\s+ai|"
    r"ai\s+pc\s+vs\s+macbook|windows\s+copilot\+?\s*pc|"
    r"foldable\s+macbook|macbook\s+ai\b(?!r)"
    r")\b",
    re.IGNORECASE,
)

_PREFERRED_DOMAINS = frozenset(
    {
        "apple.com",
        "macrumors.com",
        "9to5mac.com",
        "theverge.com",
        "pcmag.com",
        "zdnet.com",
        "nytimes.com",
        "wirecutter.com",
        "tomsguide.com",
        "techradar.com",
        "arstechnica.com",
    }
)

_LOW_QUALITY_DOMAINS = frozenset(
    {
        "pinterest.com",
        "quora.com",
        "reddit.com",
        "facebook.com",
    }
)

# Product pair definitions: (slug_a, slug_b, label_a, label_b, detect patterns)
_PRODUCT_PAIRS: list[dict] = [
    {
        "slug_a": "macbook air",
        "slug_b": "macbook pro",
        "label_a": "MacBook Air",
        "label_b": "MacBook Pro",
        "patterns": [
            re.compile(r"macbook\s+air", re.I),
            re.compile(r"macbook\s+pro", re.I),
            re.compile(r"\bm[2345]\s+air\b", re.I),
            re.compile(r"\bm[2345]\s+pro\b", re.I),
        ],
    },
    {
        "slug_a": "iphone 16",
        "slug_b": "iphone 17",
        "label_a": "iPhone 16",
        "label_b": "iPhone 17",
        "patterns": [
            re.compile(r"iphone\s*16", re.I),
            re.compile(r"iphone\s*17", re.I),
        ],
    },
    {
        "slug_a": "pixel",
        "slug_b": "iphone",
        "label_a": "Google Pixel",
        "label_b": "iPhone",
        "patterns": [
            re.compile(r"\bpixel\b", re.I),
            re.compile(r"\biphone\b", re.I),
        ],
    },
    {
        "slug_a": "ps5",
        "slug_b": "xbox series x",
        "label_a": "PlayStation 5",
        "label_b": "Xbox Series X",
        "patterns": [
            re.compile(r"\bps5\b|playstation\s*5", re.I),
            re.compile(r"xbox\s+series\s+x", re.I),
        ],
    },
]

_AIR_EVIDENCE_TERMS = frozenset(
    {
        "macbook air",
        "13-inch air",
        "15-inch air",
        "13 inch air",
        "15 inch air",
        "m2 air",
        "m3 air",
        "m4 air",
        "m5 air",
        "apple.com/macbook-air",
    }
)

_PRO_EVIDENCE_TERMS = frozenset(
    {
        "macbook pro",
        "14-inch pro",
        "16-inch pro",
        "14 inch pro",
        "16 inch pro",
        "m3 pro",
        "m4 pro",
        "m5 pro",
        "m3 max",
        "m4 max",
        "m5 max",
        "apple.com/macbook-pro",
    }
)

_REJECT_MACBOOK_EVIDENCE = frozenset(
    {
        "macbook neo",
        "macbook ai",
        "best macbook for ai",
        "windows ai pc",
        "copilot+ pc",
        "ai laptop",
    }
)

PRODUCT_COMPARISON_TABLE_DIMENSIONS = [
    "Product role",
    "Performance",
    "Battery life",
    "Display",
    "Ports",
    "Weight/portability",
    "RAM/storage options",
    "Price/value",
    "Best for",
]


@dataclass
class QueryNormalization:
    original_query: str
    effective_query: str
    normalized_query: str | None = None
    normalization_reason: str | None = None


@dataclass
class ProductIntent:
    topic_intent: str
    entities: list[str]
    normalized_entities: dict[str, str] = field(default_factory=dict)
    comparison_query: bool = True
    pricing_focus: bool = False
    ai_workload_focus: bool = False
    original_query: str = ""
    normalized_query: str | None = None
    normalization_reason: str | None = None
    product_pair: str | None = None


def normalize_user_query(user_query: str) -> QueryNormalization:
    """Normalize likely typos before planning. Returns effective query for pipeline."""
    original = user_query.strip()
    if not original:
        return QueryNormalization(original_query=original, effective_query=original)

    if _AI_WORKLOAD_EXPLICIT.search(original):
        # Explicit AI workload — never rewrite ai → air
        if _MACBOOK_AI_TYPO.search(original) or _MACBOOK_PRO_AI_TYPO.search(original):
            # "macbook pro for local AI vs macbook air" — already has air, or comparison with AI focus
            effective = re.sub(r"macbook\s+ai\b", "macbook air", original, flags=re.IGNORECASE)
            if effective.lower() != original.lower():
                return QueryNormalization(
                    original_query=original,
                    effective_query=effective,
                    normalized_query="MacBook Air vs MacBook Pro",
                    normalization_reason="likely typo ai -> air in MacBook comparison",
                )
        return QueryNormalization(original_query=original, effective_query=original)

    if _MACBOOK_AI_TYPO.search(original) or _MACBOOK_PRO_AI_TYPO.search(original):
        effective = re.sub(r"macbook\s+ai\b", "macbook air", original, flags=re.IGNORECASE)
        return QueryNormalization(
            original_query=original,
            effective_query=effective,
            normalized_query="MacBook Air vs MacBook Pro",
            normalization_reason="likely typo ai -> air in MacBook comparison",
        )

    return QueryNormalization(original_query=original, effective_query=original)


def classify_product_intent(user_query: str, original_query: str = "") -> ProductIntent | None:
    """Detect product comparison or AI-workload MacBook queries."""
    q = user_query.strip()
    if not q:
        return None

    orig = original_query or q
    norm = normalize_user_query(orig)
    effective = norm.effective_query

    ai_workload = bool(_AI_WORKLOAD_EXPLICIT.search(effective))
    is_comparison = bool(_COMPARISON_SIGNAL.search(effective))

    # "best macbook for AI" — AI workload recommendation, not Air vs Pro typo
    if ai_workload and not is_comparison:
        if re.search(r"\bmacbook\b", effective, re.I):
            return ProductIntent(
                topic_intent=TOPIC_PRODUCT_COMPARISON,
                entities=["macbook"],
                normalized_entities={"macbook": "MacBook (AI workload suitability)"},
                comparison_query=False,
                ai_workload_focus=True,
                original_query=orig,
                normalized_query=norm.normalized_query,
                normalization_reason=norm.normalization_reason,
                product_pair="macbook_ai_workload",
            )
        return None

    pair = _detect_product_pair(effective)
    if pair:
        slug_a, slug_b, label_a, label_b = pair
        return ProductIntent(
            topic_intent=TOPIC_PRODUCT_COMPARISON,
            entities=[slug_a, slug_b],
            normalized_entities={slug_a: label_a, slug_b: label_b},
            comparison_query=True,
            pricing_focus=bool(_PRICING_SIGNAL.search(effective)),
            ai_workload_focus=ai_workload,
            original_query=orig,
            normalized_query=norm.normalized_query or f"{label_a} vs {label_b}",
            normalization_reason=norm.normalization_reason,
            product_pair=f"{slug_a}_vs_{slug_b}",
        )

    return None


def _detect_product_pair(query: str) -> tuple[str, str, str, str] | None:
    q = query.lower()
    if not _COMPARISON_SIGNAL.search(q) and " or " not in q:
        return None

    for pair_def in _PRODUCT_PAIRS:
        hits = [p.search(q) for p in pair_def["patterns"]]
        if sum(1 for h in hits if h) >= 2:
            return (
                pair_def["slug_a"],
                pair_def["slug_b"],
                pair_def["label_a"],
                pair_def["label_b"],
            )
    return None


def is_offtopic_product_query(query: str) -> bool:
    return bool(_OFFTOPIC_QUERY_PATTERNS.search(query))


def filter_offtopic_product_queries(queries: list[str]) -> list[str]:
    return [q for q in queries if not is_offtopic_product_query(q)]


def build_product_plan(intent: ProductIntent, user_query: str) -> dict:
    """Deterministic plan for product comparisons."""
    if intent.product_pair == "macbook_ai_workload":
        return _macbook_ai_workload_plan(intent)
    if intent.product_pair and intent.product_pair.startswith("macbook"):
        return _macbook_air_pro_plan(intent, user_query)
    return _generic_product_plan(intent, user_query)


def _macbook_air_pro_plan(intent: ProductIntent, user_query: str) -> dict:
    entities = list(intent.normalized_entities.values())
    air = intent.normalized_entities.get("macbook air", "MacBook Air")
    pro = intent.normalized_entities.get("macbook pro", "MacBook Pro")

    subquestions = [
        f"What are the official specs and product role of the {air}?",
        f"What are the official specs and product role of the {pro}?",
        f"How do {air} and {pro} compare on performance, battery life, display, and ports?",
        f"Which is better for portability vs sustained performance?",
        "What are the current RAM/storage and pricing options?",
    ]
    if intent.ai_workload_focus:
        subquestions.append(f"How do {air} and {pro} compare for local AI/ML workloads?")
    if intent.pricing_focus or "india" in user_query.lower():
        subquestions.append(f"How do {air} and {pro} pricing compare in India?")

    queries = [
        "Apple MacBook Air official specs site:apple.com",
        "Apple MacBook Pro official specs site:apple.com",
        "MacBook Air vs MacBook Pro comparison",
        "MacBook Air vs MacBook Pro battery life",
        "MacBook Air vs MacBook Pro performance benchmark",
        "MacBook Air vs MacBook Pro ports display weight",
        "MacBook Air vs MacBook Pro buyer guide MacRumors",
        "MacBook Air vs MacBook Pro Wirecutter",
    ]
    if intent.pricing_focus or "india" in user_query.lower():
        queries.insert(2, "MacBook Air vs MacBook Pro pricing India")

    objective = (
        f"Compare {air} vs {pro} as Apple laptop products — "
        "NOT MacBook AI, MacBook Neo, generic AI laptops, or rumor/future products."
    )
    if intent.normalization_reason:
        objective += f" (User query normalized from typo: {intent.original_query})"

    return {
        "objective": objective,
        "subquestions": subquestions[:8],
        "queries": list(dict.fromkeys(queries)),
        "freshness_required": True,
        "source_preferences": [
            "apple.com MacBook Air and MacBook Pro",
            "Apple compare pages",
            "MacRumors buyer guides",
            "9to5Mac, The Verge, PCMag, ZDNET, Wirecutter",
        ],
        "expected_output": "comparison",
    }


def _macbook_ai_workload_plan(intent: ProductIntent) -> dict:
    return {
        "objective": (
            "Research which MacBook models are best suited for AI/ML workloads "
            "(local LLM inference, machine learning) — NOT a MacBook Air vs Pro typo fix."
        ),
        "subquestions": [
            "Which MacBook models support local AI/ML workloads well?",
            "How much RAM and GPU do AI workloads on MacBook require?",
            "MacBook Air vs MacBook Pro for local AI — tradeoffs?",
            "What do official Apple and reputable sources say about AI on Mac?",
        ],
        "queries": [
            "best MacBook for AI machine learning 2025 2026",
            "MacBook Pro M4 M5 local AI LLM performance",
            "MacBook Air vs Pro for AI workloads",
            "Apple MacBook neural engine machine learning official",
            "MacBook unified memory AI model size requirements",
        ],
        "freshness_required": True,
        "source_preferences": ["apple.com", "MacRumors", "9to5Mac", "The Verge"],
        "expected_output": "recommendation",
    }


def _generic_product_plan(intent: ProductIntent, user_query: str) -> dict:
    entities = list(intent.normalized_entities.values())
    a = entities[0] if entities else "Product A"
    b = entities[1] if len(entities) > 1 else "Product B"
    return {
        "objective": f"Compare {a} vs {b} as consumer products.",
        "subquestions": [
            f"What is {a} and what is it best for?",
            f"What is {b} and what is it best for?",
            f"How do {a} and {b} compare on specs, price, and use cases?",
        ],
        "queries": [
            f"{a} official specs",
            f"{b} official specs",
            f"{a} vs {b} comparison",
            f"{a} vs {b} review",
        ],
        "freshness_required": True,
        "source_preferences": ["official manufacturer sites", "reputable review sites"],
        "expected_output": "comparison",
    }


def product_entity_terms(intent: ProductIntent) -> list[str]:
    terms: set[str] = set()
    for slug, label in intent.normalized_entities.items():
        terms.add(label.lower())
        terms.add(slug)
        if "macbook air" in slug:
            terms.update(_AIR_EVIDENCE_TERMS)
        if "macbook pro" in slug:
            terms.update(_PRO_EVIDENCE_TERMS)
    if intent.product_pair and "macbook" in (intent.product_pair or ""):
        terms.update({"macbook", "apple"})
    return sorted(terms)


def classify_product_evidence_category(
    text: str,
    source: "ResearchSource",
    intent: ProductIntent,
) -> str:
    from app.services.research.types import ResearchSource  # noqa: F401

    url = (source.url or "").lower()
    combined = f"{source.title} {url} {text}".lower()

    for reject in _REJECT_MACBOOK_EVIDENCE:
        if reject in combined:
            if not (_mentions_air(combined, url) or _mentions_pro(combined, url)):
                return "irrelevant"

    if _is_macbook_ai_pollution(combined, intent, url):
        return "irrelevant"

    has_air = _mentions_air(combined, url)
    has_pro = _mentions_pro(combined, url)

    if has_air and has_pro:
        return "comparison_evidence"
    if has_air:
        return "air_evidence"
    if has_pro:
        return "pro_evidence"

    for slug in intent.entities:
        if slug in combined or intent.normalized_entities.get(slug, "").lower() in combined:
            return "general"

    return "irrelevant"


def _mentions_air(text: str, url: str = "") -> bool:
    if "macbook-air" in url or "/air" in url and "macbook" in url:
        return True
    if "macbook ai" in text and "macbook air" not in text:
        return False
    return any(t in text for t in _AIR_EVIDENCE_TERMS) or bool(
        re.search(r"\bmacbook\s+air\b|\b\d{2}[- ]inch\s+air\b|\bm[2345]\s+air\b", text)
    )


def _mentions_pro(text: str, url: str = "") -> bool:
    if "macbook-pro" in url or "/pro" in url and "macbook" in url:
        return True
    return any(t in text for t in _PRO_EVIDENCE_TERMS) or bool(
        re.search(r"\bmacbook\s+pro\b|\b\d{2}[- ]inch\s+pro\b|\bm[2345]\s+(pro|max)\b", text)
    )


def _is_macbook_ai_pollution(text: str, intent: ProductIntent, url: str = "") -> bool:
    if intent.ai_workload_focus:
        return False
    if re.search(r"\bmacbook\s+ai\b", text) and not _mentions_air(text, url):
        return True
    if "macbook neo" in text:
        return True
    if re.search(r"best\s+macbook\s+for\s+ai", text) and not (
        _mentions_air(text, url) and _mentions_pro(text, url)
    ):
        return True
    return False


def source_is_offtopic_for_product(source: "ResearchSource", intent: ProductIntent) -> str | None:
    from app.services.research.types import ResearchSource  # noqa: F401

    title = (source.title or "").lower()
    url = (source.url or "").lower()
    sample = (source.text or "")[:5000].lower()
    combined = f"{title} {url} {sample}"

    if intent.ai_workload_focus:
        return None

    if _OFFTOPIC_SOURCE_PATTERNS.search(combined):
        if not (_mentions_air(combined, url) and _mentions_pro(combined, url)):
            return "Irrelevant source (MacBook AI/Neo/AI laptop pollution)"

    if re.search(r"\bmacbook\s+ai\b", title) and not _mentions_air(combined, url):
        return "Irrelevant title (MacBook AI treated as product)"

    if "macbook neo" in combined and "macbook air" not in combined:
        return "Irrelevant source (MacBook Neo rumor)"

    if re.search(r"\b(m1\s+macbook|2018\s+macbook)\b", combined):
        if not re.search(r"\b(m[2-5]|2023|2024|2025|2026)\b", combined):
            if "apple.com" not in domain:
                return "Outdated MacBook comparison (pre-current generation)"

    return None


def is_preferred_product_source(source: "ResearchSource", intent: ProductIntent) -> bool:
    domain = (source.domain or "").lower()
    url = (source.url or "").lower()
    if "apple.com" in domain and ("macbook-air" in url or "macbook-pro" in url or "macbook" in url):
        return True
    return any(d in domain for d in _PREFERRED_DOMAINS)


def is_low_quality_product_source(source: "ResearchSource") -> bool:
    domain = (source.domain or "").lower()
    return any(d in domain for d in _LOW_QUALITY_DOMAINS)


def intent_from_topic(intent: "TopicIntent") -> ProductIntent:
    """Convert unified TopicIntent back to ProductIntent for product-specific helpers."""
    from app.services.research.topic_intent import TopicIntent  # noqa: F401

    return ProductIntent(
        topic_intent=intent.topic_intent,
        entities=intent.tools,
        normalized_entities=intent.normalized_entities,
        comparison_query=intent.comparison_query,
        pricing_focus=intent.pricing_focus,
        ai_workload_focus=intent.ai_workload_focus,
        original_query=intent.original_query,
        normalized_query=intent.normalized_query,
        normalization_reason=intent.normalization_reason,
        product_pair=intent.product_pair,
    )


def product_has_source_pollution(sources: list, evidence: list) -> bool:
    """True if AI/Neo pollution appears in used sources."""
    for src in sources:
        if getattr(src, "evidence_count", 0) <= 0:
            continue
        combined = f"{getattr(src, 'title', '')} {getattr(src, 'url', '')}".lower()
        if re.search(r"macbook\s+ai\b|macbook\s+neo|best\s+macbook\s+for\s+ai", combined):
            if "macbook air" not in combined:
                return True
    return False
