"""Model-based search query planning.

The provider query used to be produced by a long ladder of hand-written regex
rules, one per entity Neo had been asked about.  That ladder only ever fired
for the entities somebody had already hit a bug with, and it left ordinary
questions untouched -- so a
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

from app.services.current_time import today_for_prompt
from app.services.disclosure import Label, content_hash, label_of, public, to_json
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
Rewrite one user message into web search queries.

Return exactly one JSON object on one line, with no Markdown and no extra text:
{"q":"search keywords","q2":"","terms":["topic"],"mode":"fact|news|overview",\
"window":"day"|"week"|"month"|"year"|null}

Fields:
- q: the keywords a person would type into a search box. Drop question words,
  politeness and filler. Keep names, titles, product names, distinctive phrases,
  years and version numbers exactly as the user wrote them, fixing only obvious typos,
  even when a name looks like ordinary words. Never invent an entity the user did not
  mention. Never add words like "official" or "release date" unless the user asked for a
  date. When the user asks for the latest, newest, last, current or most recent one of
  something, add the current year to q, so that pages about older ones rank lower.
- q2: a second, different query only when the message asks for two distinct things that
  one search would not find together (two separate subjects, or what already happened
  and what is planned next). Otherwise "".
- terms: the 1-3 lowercase nouns naming the topic. Only words that would
  plausibly appear in the title of a correct result. Never include words like
  changed, latest, new, recent, official, version, update.
- mode: fact for a single fact (a date, price, number or name); news for what is
  new, happening or planned with something, or its current state; overview for an
  explanation, plot, summary, or a list of recommendations.
- window: day or week only when an older answer would be wrong; month or year when the
  question is about the current state of something, the latest or upcoming ones, or
  this year, and older pages would be out of date. Use null for changelogs,
  documentation and anything historical.

When a line "USER'S OWN WORDS:" follows the message, the message is a rewrite of what the
user said. Keep the names and phrases the user used.

Examples (placeholders stand for whatever the user actually names):
USER: what changed in the newest release of <software>
JSON: {"q":"<software> release notes changelog","q2":"","terms":["<software>"],"mode":"news",\
"window":null}
USER: when does <title> come out in <country>
JSON: {"q":"<title> <country> release date","q2":"","terms":["<title>"],"mode":"fact","window":null}
USER: what is the price of <product> right now
JSON: {"q":"<product> price","q2":"","terms":["<product>"],"mode":"fact","window":"day"}
USER: how many <units> does <work> have
JSON: {"q":"<work> number of <units>","q2":"","terms":["<work>"],"mode":"fact","window":null}
USER: tell me about the story of <work>
JSON: {"q":"<work> plot summary","q2":"","terms":["<work>"],"mode":"overview","window":null}
USER: what are the best <things> for <purpose>
JSON: {"q":"best <things> for <purpose>","q2":"","terms":["<things>"],"mode":"overview",\
"window":null}
USER: who won the most recent <event>
JSON: {"q":"<event> winner <current year>","q2":"","terms":["<event>"],"mode":"fact","window":null}
USER: what is going on with <thing> lately
JSON: {"q":"<thing> latest news <current year>","q2":"","terms":["<thing>"],"mode":"news",\
"window":"month"}
USER: what are the latest <things> from <maker> and what is coming next
JSON: {"q":"<maker> latest <things> <current year>","q2":"<maker> upcoming <things>",\
"terms":["<maker>"],"mode":"news","window":"year"}
"""

_PLAN_JSON = re.compile(r"\{\s*\"q\".*?\}", re.DOTALL)


class SearchPlan(BaseModel):
    """How one user turn should be searched for."""

    provider_query: str
    subject_terms: list[str] = Field(default_factory=list)
    answer_mode: str = "unknown"
    time_filter: str | None = None
    source: str = "fallback"
    #: A second query for a distinct part of the question, when the planner named one
    #: and it passed validation. Whether it is run is decided after the first search.
    secondary_query: str | None = None


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
        self._cache: OrderedDict[str, SearchPlan] = OrderedDict()

    def plan(
        self,
        query: str,
        *,
        llm: LLMClient | None = None,
        hint: str | None = None,
        user_text: str | None = None,
        source_label: Label | None = None,
    ) -> SearchPlan:
        """Plan ``query``.

        ``hint`` is the structured resolver's rewritten query (for example the
        entity it pulled out of a release-date question).  It is offered to the
        model as extra context but never used as the search string on its own.
        ``user_text`` is the user's own message when ``query`` is a rewrite of it
        (the router's standalone query): the planner sees both, so a name or
        phrase the rewrite dropped is not lost.
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

        source_label = label_of(source_label)
        cache_key = content_hash({
            "query": query, "user_text": user_text, "hint": hint,
            "label": to_json(source_label), "model": getattr(llm, "model", ""),
            "date": today_for_prompt(),
        })
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cached

        payload = self._request_plan(query, llm, hint, user_text, source_label=source_label)
        plan = self._validate(payload, query, user_text) if payload is not None else None
        result = plan or fallback
        if plan is not None:
            self._remember(cache_key, plan)
        return result

    @staticmethod
    def _request_plan(
        query: str, llm: LLMClient, hint: str | None, user_text: str | None = None,
        *, source_label: Label | None = None,
    ) -> dict[str, object] | None:
        user_content = query if not hint or hint == query else f"{query}\n\n(topic: {hint})"
        if user_text and " ".join(user_text.split()) != " ".join(query.split()):
            user_content = f"{user_content}\nUSER'S OWN WORDS: {' '.join(user_text.split())[:400]}"
        today = today_for_prompt()
        try:
            raw = llm.chat(
                [
                    LLMMessage(
                        role="system",
                        source_label=public("builtin:search_query_planner"),
                        content=(
                            _QUERY_PLAN_SYSTEM_PROMPT
                            + f"\nToday's date is {today}, so the current year is {today[:4]}. "
                            "Resolve words like today, this week and this year relative to "
                            "this date. Your own memory of recent events stops earlier, so "
                            "never put a year you remember as the latest into q; use the "
                            "current year.\n"
                        ),
                    ),
                    LLMMessage(
                        role="user", content=user_content, source_label=label_of(source_label),
                    ),
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
    def _validate(
        payload: dict[str, object], query: str, user_text: str | None = None
    ) -> SearchPlan | None:
        raw_query = payload.get("q")
        if not isinstance(raw_query, str):
            return None
        asked_text = f"{query} {user_text or ''}"
        provider_query = _validated_query(raw_query, asked_text)
        if provider_query is None:
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

        secondary: str | None = None
        raw_secondary = payload.get("q2")
        if isinstance(raw_secondary, str) and raw_secondary.strip():
            candidate = _validated_query(raw_secondary, asked_text)
            if candidate is not None and _distinct_need(provider_query, candidate, subject_terms):
                secondary = candidate

        return SearchPlan(
            provider_query=provider_query,
            subject_terms=subject_terms,
            answer_mode=mode or "unknown",
            time_filter=window,
            source="model",
            secondary_query=secondary,
        )

    def _remember(self, key: str, plan: SearchPlan) -> None:
        self._cache[key] = plan
        self._cache.move_to_end(key)
        while len(self._cache) > _PLAN_CACHE_MAX_ENTRIES:
            self._cache.popitem(last=False)


def _validated_query(raw: str, asked: str) -> str | None:
    """A planned query, or ``None`` when it drifted, is too long, or lost a requested year.

    A rewrite that shares no content word with what was asked has drifted off topic (or
    hallucinated an entity). A year can be the whole point of a buying or release question,
    so one the user asked for must survive. A year older than last year that the user never
    wrote can only come from the model's memory, which ends at its training data, so it is
    removed.
    """

    provider_query = " ".join(raw.split())
    if not provider_query or len(provider_query) > MAX_PROVIDER_QUERY_CHARS:
        return None
    asked_tokens = content_tokens(asked)
    if asked_tokens and not (asked_tokens & content_tokens(provider_query)):
        return None
    requested_years = set(re.findall(r"\b(?:19|20)\d{2}\b", asked))
    planned_years = set(re.findall(r"\b(?:19|20)\d{2}\b", provider_query))
    if requested_years - planned_years:
        return None
    oldest_resolvable = int(today_for_prompt()[:4]) - 1
    for year in planned_years - requested_years:
        if int(year) < oldest_resolvable:
            provider_query = " ".join(re.sub(rf"\b{year}\b", " ", provider_query).split())
    return provider_query or None


def _distinct_need(first: str, second: str, subject_terms: list[str]) -> bool:
    """Whether ``second`` asks for something ``first`` does not, about the same subject.

    A near-duplicate wording would only repeat the first search; a query that drops the
    subject searches for something else.
    """

    first_words, second_words = query_words(first), query_words(second)
    if not second_words:
        return False
    overlap = len(first_words & second_words) / len(first_words | second_words)
    if overlap >= 0.75:
        return False
    subject = {word for term in subject_terms for word in query_words(term)}
    anchor = subject or content_tokens(first)
    return bool(anchor & second_words)


#: Function words only. Distinctness compares every other word: "latest" and "upcoming"
#: are exactly what can make a second query ask something the first did not.
_FUNCTION_WORDS = frozenset(
    "a an and are as at be by for from how in is it of on or that the this to was what when "
    "where which who why with".split()
)


def query_words(query: str) -> set[str]:
    return {
        word
        for word in re.findall(r"[a-z0-9][a-z0-9.+#-]*", (query or "").lower())
        if word not in _FUNCTION_WORDS and not re.fullmatch(r"(?:19|20)\d{2}", word)
    }


_PLANNER = QueryPlanner()


def plan_query(
    query: str,
    *,
    llm: LLMClient | None = None,
    hint: str | None = None,
    user_text: str | None = None,
    source_label: Label | None = None,
) -> SearchPlan:
    """Plan ``query`` using the process-wide planner (and its cache)."""
    return _PLANNER.plan(
        query, llm=llm, hint=hint, user_text=user_text, source_label=source_label,
    )
