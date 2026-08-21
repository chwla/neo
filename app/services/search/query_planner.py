"""Model-based search query planning.

The provider query used to be produced by a long ladder of hand-written regex
rules, one per entity Neo had been asked about (Avengers Doomsday, Spider-Man,
Dune 3, FIDE, BCCI, ...).  That ladder only ever fired for the entities somebody
had already hit a bug with, and it left ordinary questions untouched -- so a
natural-language question was passed to the provider verbatim, and every word in
it was then treated as a relevance term.

``QueryPlanner`` replaces the ladder with one small JSON-only model call that
turns a user turn into a keyword query plus the metadata the rest of the
pipeline needs.  The model is advisory: every field is validated, and any
failure (no client, no JSON, topic drift, bad enum) falls back to
``fallback_provider_query`` below, which keeps the *generic* cleanups from the
old function without any per-entity special cases.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from app.services.llm import LLMMessage

if TYPE_CHECKING:
    from app.services.llm import LLMClient

_MODE_ALIASES = {
    "fact": "fact_lookup",
    "fact_lookup": "fact_lookup",
    "news": "news_summary",
    "news_summary": "news_summary",
    "overview": "overview",
}
TIME_FILTERS = {"day", "week", "month", "year"}

MAX_PROVIDER_QUERY_CHARS = 128
_PLAN_CACHE_MAX_ENTRIES = 256

# Words that carry no discriminating power when matched against a page title or
# snippet.  A query is not "about" them, so they must not be able to raise the
# relevance bar in ``rank_results`` (see NON_DISCRIMINATIVE_TERMS in ranking.py)
# and they are not enough, on their own, to keep a model rewrite on topic.
_CONTENT_STOPWORDS = {
    "about",
    "added",
    "again",
    "against",
    "all",
    "am",
    "an",
    "and",
    "announced",
    "another",
    "any",
    "anything",
    "are",
    "around",
    "as",
    "at",
    "back",
    "because",
    "been",
    "before",
    "being",
    "below",
    "best",
    "between",
    "big",
    "both",
    "but",
    "by",
    "can",
    "change",
    "changed",
    "changes",
    "come",
    "coming",
    "could",
    "current",
    "currently",
    "date",
    "details",
    "did",
    "difference",
    "differences",
    "different",
    "does",
    "down",
    "during",
    "each",
    "else",
    "even",
    "ever",
    "every",
    "few",
    "for",
    "from",
    "further",
    "get",
    "give",
    "had",
    "has",
    "have",
    "he",
    "her",
    "here",
    "hers",
    "him",
    "his",
    "how",
    "if",
    "improved",
    "improvement",
    "improvements",
    "in",
    "info",
    "information",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "known",
    "last",
    "late",
    "latest",
    "let",
    "like",
    "list",
    "look",
    "made",
    "main",
    "major",
    "make",
    "many",
    "may",
    "me",
    "might",
    "more",
    "most",
    "much",
    "must",
    "my",
    "near",
    "need",
    "new",
    "newest",
    "news",
    "nor",
    "not",
    "now",
    "off",
    "official",
    "officially",
    "old",
    "on",
    "once",
    "only",
    "or",
    "other",
    "others",
    "ought",
    "our",
    "ours",
    "out",
    "over",
    "own",
    "per",
    "please",
    "recent",
    "recently",
    "release",
    "released",
    "releases",
    "releasing",
    "same",
    "search",
    "see",
    "shall",
    "she",
    "should",
    "show",
    "since",
    "so",
    "some",
    "still",
    "stuff",
    "such",
    "tell",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "things",
    "this",
    "those",
    "though",
    "through",
    "till",
    "to",
    "too",
    "under",
    "until",
    "up",
    "upcoming",
    "update",
    "updated",
    "updates",
    "upon",
    "us",
    "use",
    "used",
    "version",
    "versions",
    "very",
    "via",
    "want",
    "was",
    "we",
    "well",
    "were",
    "what",
    "whats",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "whose",
    "why",
    "will",
    "with",
    "within",
    "without",
    "would",
    "yet",
    "you",
    "your",
}

_QUERY_PLAN_SYSTEM_PROMPT = """You are Neo's search query planner.
Rewrite one user message into a web search query.

Return exactly one JSON object on one line, with no Markdown and no extra text:
{"q":"search keywords","terms":["topic"],"mode":"fact|news|overview","window":"day"|"week"|null}

Fields:
- q: the keywords a person would type into a search box. Drop question words,
  politeness and filler. Keep proper nouns, product names and version numbers
  exactly as written. Never invent an entity the user did not mention. Never add
  words like "official" or "release date" unless the user asked for a date.
- terms: the 1-3 lowercase nouns naming the topic. Only words that would
  plausibly appear in the title of a correct result. Never include words like
  changed, latest, new, recent, official, version, update.
- mode: fact for a single fact (a date, price, number or name); news for what is
  new or what happened recently; overview for an explanation, plot or summary.
- window: day or week only when an older answer would be wrong. Use null for
  changelogs, documentation and anything historical.

Examples:
USER: What changed in the latest React release?
JSON: {"q":"React release notes changelog","terms":["react"],"mode":"news","window":null}
USER: when is Avengers Doomsday releasing in India
JSON: {"q":"Avengers Doomsday India release date","terms":["avengers"],"mode":"fact","window":null}
USER: search what has recently changed in react
JSON: {"q":"React recent changes release notes","terms":["react"],"mode":"news","window":null}
USER: what is the current price of NVIDIA stock
JSON: {"q":"NVIDIA stock price","terms":["nvidia"],"mode":"fact","window":"day"}
USER: how many seasons does Invincible have
JSON: {"q":"Invincible season count","terms":["invincible"],"mode":"fact","window":null}
USER: tell me about the plot of Dune Part Three
JSON: {"q":"Dune Part Three plot","terms":["dune"],"mode":"overview","window":null}
"""

_PLAN_JSON = re.compile(r"\{\s*\"q\".*?\}", re.DOTALL)


class SearchPlan(BaseModel):
    """How one user turn should be searched for."""

    provider_query: str
    subject_terms: list[str] = Field(default_factory=list)
    answer_mode: str = "unknown"
    time_filter: str | None = None
    source: str = "fallback"


def content_tokens(value: str) -> set[str]:
    """Lowercase tokens from ``value`` that actually identify a topic."""
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+#-]*", value.lower())
        if len(token) > 1 and token not in _CONTENT_STOPWORDS
    }


def fallback_provider_query(query: str) -> str:
    """Deterministic query cleanup used whenever the planner is unavailable.

    This is the generic head of the old ``provider_query`` ladder: it strips
    greetings, explicit search commands and question stems, and nothing else.
    The per-entity rules it used to carry are gone -- the planner handles those.
    """
    cleaned = " ".join(query.split())
    cleaned = re.sub(r"^(hi|hello|hey)\s+neo[:,\s-]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(hi|hello|hey)[:,\s-]*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(can you |could you |please )?"
        r"(search|search the web|search online|look up|lookup|find|google)"
        r"( for| about)?[:,\s-]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(please\s+)?("
        r"look up|lookup|search the web for|search web for|search for|web search for|"
        r"verify|fact check"
        r")\b[:,\s-]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^(what|when|where|who|how)\s+is\s+the\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"^(what|when|where|who|how)\s+(?:is|are|does|do)\s+", "", cleaned, flags=re.IGNORECASE
    )
    cleaned = cleaned.strip(" .?!")
    return cleaned or " ".join(query.split())


class QueryPlanner:
    """Turn a user turn into a provider query via the selected chat model."""

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, str], SearchPlan] = OrderedDict()

    def plan(
        self,
        query: str,
        *,
        llm: LLMClient | None = None,
        hint: str | None = None,
    ) -> SearchPlan:
        """Plan ``query``.

        ``hint`` is the structured resolver's rewritten query (for example the
        entity it pulled out of a release-date question).  It is offered to the
        model as extra context but never used as the search string on its own.
        """
        # subject_terms is deliberately left empty on the fallback path: it is a
        # narrow claim ("these 1-3 nouns are the topic") that only the model is in
        # a position to make. Guessing it here would loosen the ranking gate,
        # which falls back to NON_DISCRIMINATIVE_TERMS instead.
        fallback = SearchPlan(
            provider_query=fallback_provider_query(query),
            source="fallback",
        )
        if llm is None or not query.strip():
            return fallback

        cache_key = (" ".join(query.lower().split()), getattr(llm, "model", ""))
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        payload = self._request_plan(query, llm, hint)
        plan = self._validate(payload, query) if payload is not None else None
        result = plan or fallback
        if plan is not None:
            self._remember(cache_key, plan)
        return result

    @staticmethod
    def _request_plan(
        query: str, llm: LLMClient, hint: str | None
    ) -> dict[str, object] | None:
        user_content = query if not hint or hint == query else f"{query}\n\n(topic: {hint})"
        try:
            raw = llm.chat(
                [
                    LLMMessage(role="system", content=_QUERY_PLAN_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=user_content),
                ],
                temperature=0.0,
            )
            cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
            match = _PLAN_JSON.search(cleaned)
            payload = json.loads(match.group(0) if match else cleaned.strip())
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _validate(payload: dict[str, object], query: str) -> SearchPlan | None:
        raw_query = payload.get("q")
        if not isinstance(raw_query, str):
            return None
        provider_query = " ".join(raw_query.split())
        if not provider_query or len(provider_query) > MAX_PROVIDER_QUERY_CHARS:
            return None

        # A rewrite that shares no content word with the question has drifted off
        # topic (or hallucinated an entity); the deterministic cleanup is safer.
        asked = content_tokens(query)
        if asked and not (asked & content_tokens(provider_query)):
            return None

        raw_terms = payload.get("terms")
        subject_terms: list[str] = []
        if isinstance(raw_terms, list):
            for term in raw_terms:
                if not isinstance(term, str):
                    continue
                normalized = term.strip().lower()
                if normalized and normalized not in _CONTENT_STOPWORDS:
                    subject_terms.append(normalized)
        subject_terms = list(dict.fromkeys(subject_terms))[:3]

        raw_mode = payload.get("mode")
        mode = _MODE_ALIASES.get(raw_mode.strip().lower()) if isinstance(raw_mode, str) else None

        raw_window = payload.get("window")
        window = raw_window if isinstance(raw_window, str) and raw_window in TIME_FILTERS else None

        return SearchPlan(
            provider_query=provider_query,
            subject_terms=subject_terms,
            answer_mode=mode or "unknown",
            time_filter=window,
            source="model",
        )

    def _remember(self, key: tuple[str, str], plan: SearchPlan) -> None:
        self._cache[key] = plan
        self._cache.move_to_end(key)
        while len(self._cache) > _PLAN_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)


_PLANNER = QueryPlanner()


def plan_query(
    query: str, *, llm: LLMClient | None = None, hint: str | None = None
) -> SearchPlan:
    """Plan ``query`` using the process-wide planner (and its cache)."""
    return _PLANNER.plan(query, llm=llm, hint=hint)
