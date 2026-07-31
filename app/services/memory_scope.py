from __future__ import annotations

import re
from collections.abc import Callable, Iterable

_GENERIC_TOPIC_WORDS = {
    "about",
    "advice",
    "all",
    "answer",
    "answers",
    "better",
    "beginner",
    "checklist",
    "chat",
    "chats",
    "current",
    "daily",
    "estimate",
    "every",
    "future",
    "general",
    "give",
    "goal",
    "goals",
    "guidance",
    "help",
    "improve",
    "keep",
    "learn",
    "learning",
    "memory",
    "memories",
    "notes",
    "only",
    "organize",
    "plan",
    "plans",
    "practice",
    "prefer",
    "preferred",
    "preference",
    "preferences",
    "recommendation",
    "recommendations",
    "remember",
    "reminder",
    "reminders",
    "response",
    "responses",
    "review",
    "saved",
    "settings",
    "short",
    "simple",
    "step",
    "steps",
    "time",
    "tips",
    "tomorrow",
    "too",
    "use",
    "weekly",
    "want",
}

_STOP_WORDS = _GENERIC_TOPIC_WORDS | {
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "do",
    "for",
    "from",
    "get",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "with",
    "you",
}

_GLOBAL_RESPONSE_STYLE_PATTERNS = (
    re.compile(r"\ball answers?\b", re.IGNORECASE),
    re.compile(r"\bevery (?:answer|response|topic)\b", re.IGNORECASE),
    re.compile(r"\bfor every topic\b", re.IGNORECASE),
    re.compile(r"\bacross all (?:chats|topics|conversations)\b", re.IGNORECASE),
    re.compile(
        r"\bin general\s*[:,]?\s*(?:please\s+)?(?:respond|answer|write|be)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bgenerally[, ]+(?:respond|answer|write)\b", re.IGNORECASE),
    re.compile(r"\balways (?:respond|answer|write)\b", re.IGNORECASE),
)

_EXPLICIT_TOPIC_PATTERN = re.compile(
    r"(?P<topic>(?:[a-z0-9][a-z0-9+#_-]*\s+){0,4}[a-z0-9][a-z0-9+#_-]*)"
    r"\s+(?:advice|guidance|recommendations?|goals?|preferences?|"
    r"practice(?:\s+(?:plan|assignment))?|learning)\b",
    re.IGNORECASE,
)

_PREFERENCE_TOPIC_PATTERN = re.compile(
    r"^(?P<topic>[a-z0-9][a-z0-9+#_-]*(?:\s+[a-z0-9][a-z0-9+#_-]*){0,4})"
    r"\s+(?:advice|guidance|recommendations?|explanations?)\b",
    re.IGNORECASE,
)

_CANONICAL_DOMAIN_ORDER = (
    "language_learning",
    "cooking",
    "fitness",
    "home_organization",
    "travel_planning",
    "coding",
    "music",
    "gardening",
)


def is_global_response_style(text: str) -> bool:
    """Whether a preference explicitly applies across topics or conversations."""

    return any(pattern.search(text) for pattern in _GLOBAL_RESPONSE_STYLE_PATTERNS)


def canonical_domain_label(text: str) -> str | None:
    """Normalize an explicitly declared domain without discarding its modifiers."""

    return _normalize_topic_phrase(text)


def preference_domain(text: str) -> str | None:
    """Derive a stable domain label from the subject of a preference."""

    normalized = _normalize(text)
    if not normalized or is_global_response_style(normalized):
        return None

    match = _PREFERENCE_TOPIC_PATTERN.search(normalized)
    if match:
        domain = _normalize_topic_phrase(match.group("topic"))
        if domain:
            return domain

    aliases = _canonical_aliases(normalized)
    if aliases:
        return _preferred_alias(aliases)
    return None


def primary_domain_for_text(text: str) -> str | None:
    """Choose one stable domain label while retaining general token matching."""

    normalized = _normalize(text)
    aliases = _canonical_aliases(normalized)
    if aliases:
        return _preferred_alias(aliases)

    phrases = _explicit_topic_phrases(normalized)
    if phrases:
        return _normalize_topic_phrase(phrases[0])

    tokens = _meaningful_tokens(normalized)
    return tokens[-1] if tokens else None


def domains_for_text(text: str) -> frozenset[str]:
    """Build a universal topic signature from phrases, aliases, and entities.

    Canonical aliases normalize a small set of common equivalents. Arbitrary unseen
    topics are retained through their explicit noun phrases and meaningful tokens.
    """

    normalized = _normalize(text)
    if not normalized:
        return frozenset()

    domains = set(_canonical_aliases(normalized))
    for match in re.finditer(
        r"\b(?:goal|preference):(?P<domain>[a-z0-9+#_-]+)(?::|\b)",
        text,
        flags=re.IGNORECASE,
    ):
        domain = canonical_domain_label(match.group("domain"))
        if domain:
            domains.add(domain)
    for match in re.finditer(
        r"\b(?P<domain>[a-z0-9+#]+(?:[-_][a-z0-9+#]+)+)\b",
        text,
        flags=re.IGNORECASE,
    ):
        domain = canonical_domain_label(match.group("domain"))
        if domain:
            domains.add(domain)
    for phrase in _explicit_topic_phrases(normalized):
        domain = _normalize_topic_phrase(phrase)
        if domain:
            domains.add(domain)
    domains.update(_meaningful_tokens(normalized))
    return frozenset(domains)


def text_matches_domains(text: str, domains: frozenset[str]) -> bool:
    """Whether text belongs to at least one requested topic signature."""

    if not domains:
        return True
    if is_global_response_style(text):
        return True
    item_domains = domains_for_text(text)
    requested_compounds = {domain for domain in domains if "_" in domain}
    if requested_compounds:
        item_compounds = {domain for domain in item_domains if "_" in domain}
        if item_compounds:
            return bool(requested_compounds & item_compounds)
        compound_parts = {
            part
            for domain in requested_compounds
            for part in domain.split("_")
        }
        requested_simple = domains - requested_compounds - compound_parts
        requested_heads = {
            domain.rsplit("_", maxsplit=1)[-1]
            for domain in requested_compounds
        }
        # A compound query can legitimately refine a simpler stored subject
        # ("street photography" -> "photography"). Conflicting compound rows were
        # already rejected above, so this fallback applies only to head-only records.
        return bool((requested_simple | requested_heads) & item_domains)
    return bool(item_domains & domains)


def scoped_items[T](
    items: Iterable[T],
    domains: frozenset[str],
    text: Callable[[T], str],
) -> list[T]:
    """Filter typed memory objects while preserving repository ordering."""

    if not domains:
        return list(items)
    return [item for item in items if text_matches_domains(text(item), domains)]


def memory_text(item: object) -> str:
    """Build a stable searchable representation for any typed memory object."""

    fields = (
        "memory_text",
        "canonical_slot",
        "category",
        "value",
        "goal",
        "description",
        "name",
        "event",
    )
    return " ".join(
        str(value) for field in fields if (value := getattr(item, field, None)) is not None
    )


def _canonical_aliases(text: str) -> set[str]:
    aliases: set[str] = set()
    words = set(_tokenize(text))

    if (
        re.search(r"\blanguage learning\b|\bforeign language\b", text)
        or text == "language"
    ) or (
        re.search(r"\b(?:learn|learning|study|speak|practice)\b", text)
        and words
        & {
            "spanish",
            "french",
            "german",
            "italian",
            "japanese",
            "mandarin",
            "hindi",
        }
    ):
        aliases.add("language_learning")
    if words & {"recipe", "recipes", "cook", "cooking"}:
        aliases.add("cooking")
    if words & {
        "fitness",
        "workout",
        "workouts",
        "exercise",
        "exercises",
        "strength",
        "stamina",
        "cardio",
        "gym",
    }:
        aliases.add("fitness")
    if (
        words & {"organization", "organizing", "organize", "organized", "declutter"}
        and words & {"home", "room", "desk", "closet", "storage"}
    ) or re.search(r"\bhome organization\b|\broom organization\b|\bdesk organization\b", text):
        aliases.add("home_organization")
    if (
        words & {"travel", "trip", "trips", "itinerary"}
        and words
        & {
            "budget",
            "plan",
            "planning",
            "weekend",
            "transport",
            "packing",
            "itinerary",
        }
    ):
        aliases.add("travel_planning")
    if words & {
        "coding",
        "programming",
        "software",
        "debugging",
        "algorithm",
        "algorithms",
        "database",
        "api",
        "bst",
    } or re.search(
        r"\btechnical explanations?\b|\bcomputer science\b|"
        r"\bdata structures?\b|\bbinary search trees?\b",
        text,
    ):
        aliases.add("coding")
    if words & {
        "music",
        "musical",
        "guitar",
        "piano",
        "violin",
        "drum",
        "drums",
        "singing",
        "songwriting",
    }:
        aliases.add("music")
    if words & {
        "garden",
        "gardening",
        "plant",
        "plants",
        "houseplant",
        "houseplants",
    }:
        aliases.add("gardening")
    return aliases


def _explicit_topic_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    for match in _EXPLICIT_TOPIC_PATTERN.finditer(text):
        phrase = _clean_topic_words(match.group("topic"))
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return phrases


def _normalize_topic_phrase(phrase: str) -> str | None:
    cleaned = _clean_topic_words(_normalize(phrase))
    if not cleaned:
        return None
    aliases = _canonical_aliases(cleaned)
    if aliases:
        return _preferred_alias(aliases)
    tokens = _meaningful_tokens(cleaned)
    if not tokens:
        return None
    # Keep the complete declared phrase so independent compound topics do not share
    # one destructive slot (for example, "video editing" and "photo editing").
    return "_".join(tokens)


def _clean_topic_words(value: str) -> str:
    words = _tokenize(value)
    while words and words[0] in _STOP_WORDS:
        words.pop(0)
    while words and words[-1] in _STOP_WORDS:
        words.pop()
    return " ".join(words)


def _meaningful_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _tokenize(text):
        normalized = _singularize(token)
        if normalized in _STOP_WORDS or len(normalized) < 3:
            continue
        if normalized not in tokens:
            tokens.append(normalized)
    return tokens


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9+#]+", _normalize(text))


def _singularize(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if (
        token.endswith("s")
        and len(token) > 4
        and not token.endswith(("ics", "ss"))
    ):
        return token[:-1]
    return token


def _preferred_alias(aliases: set[str]) -> str:
    return next(
        domain for domain in _CANONICAL_DOMAIN_ORDER if domain in aliases
    )


def _normalize(text: str) -> str:
    return " ".join(
        text.casefold().replace("_", " ").replace("-", " ").split()
    )
