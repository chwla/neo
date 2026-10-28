from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from app.services.current_time import today_for_prompt
from app.services.disclosure import Label, join, label_of, public, to_json
from app.services.llm import LLMMessage
from app.services.search.types import ResolvedSearchIntent, SearchIntentKind

if TYPE_CHECKING:
    from app.services.llm import LLMClient

_SPACE = re.compile(r"\s+")
_LOG = logging.getLogger("neo.chat.routing")
#: Fixed by where a decision came from, not reported by a model: a small model's
#: self-reported confidence is not calibrated and nothing consumed it. These keep
#: the stored field meaningful as a coarse label, and they are not probabilities.
_DETERMINISTIC_CONFIDENCE = 0.99
_MODEL_CONFIDENCE = 0.8
_FALLBACK_CONFIDENCE = 0.7
_QUESTION_WORD = re.compile(r"\b(?:who|what|when|where|why|how|which)\b", re.IGNORECASE)
# An explicit "remember ..." instruction is a memory-write command regardless of
# what follows it.  ``_PERSONAL_DECLARATION`` below only recognises specific
# shapes ("my name is", "I prefer", ...) immediately after the "remember"
# prefix, so a message like "Remember this for future chats: I want to improve
# at home coffee brewing... water temperature..." fell through to weather-signal
# matching on the word "temperature" instead of being treated as memory.
_EXPLICIT_REMEMBER_COMMAND = re.compile(r"^\s*(?:please\s+)?remember\b", re.IGNORECASE)
_EXPLICIT_LOOKUP = re.compile(
    r"(?:^\s*(?:please\s+)?(?:search|find|google)\b|"
    r"\b(?:look\s+up|lookup|verify|fact[- ]?check|check online)\b)",
    re.IGNORECASE,
)
#: A request to search with nothing else in it: it refers to the earlier
#: question, so the earlier question is what gets searched.
_BARE_SEARCH_COMMAND = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:search|web\s+search|search\s+(?:it|that|this|for\s+(?:it|that|this))|"
    r"search\s+(?:the\s+)?(?:web|internet)|search\s+online|"
    r"look\s+(?:it|this|that)\s+up|google\s+(?:it|that|this)|check\s+online)"
    r"(?:\s+(?:please|for\s+me))?[.!?\s]*$",
    re.IGNORECASE,
)
#: An explicit instruction to use the web, with a subject. Only wording that
#: names the web itself counts; "find ..." or "verify ..." alone do not, because
#: "find the bug" and "verify my proof" are not requests to search.
_EXPLICIT_WEB_COMMAND = re.compile(
    r"(?:\b(?:search|look)\s+(?:up\s+)?(?:on\s+)?(?:the\s+)?(?:web|internet)\b|"
    r"\b(?:search|check|look\s+\S+(?:\s+\S+){0,6}\s+up)\s+online\b|"
    r"\bonline\s+search\b|\bweb\s+search\b|"
    r"\bgoogle\s+(?!it\b|that\b|this\b)\S|"
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?look\s+up\s+(?!it\b|that\b|this\b)\S|"
    r"\bfact[- ]?check\b)",
    re.IGNORECASE,
)


#: Longer than this, a message is treated as carrying material beyond a single
#: request. Generous for a question, short for anything pasted.
_SINGLE_REQUEST_MAX_CHARS = 280


def _is_single_request(query: str) -> bool:
    """A short message on one or two lines: the shape of a request, not a paste."""

    text = (query or "").strip()
    return len(text) <= _SINGLE_REQUEST_MAX_CHARS and text.count("\n") <= 1


#: Words that ask how things stand at present ("... yet?", "these days", "this year").
_CURRENT_STATUS = re.compile(
    r"\b(?:yet|so far|these days|nowadays|lately|recently|right now|currently|at the moment|"
    r"this (?:year|month|week|season|summer|winter|spring|fall|autumn)|latest|newest|"
    r"most recent|any new|anything new)\b",
    re.IGNORECASE,
)
#: The user speaking about themselves: "a friend I haven't seen recently", "the latest time
#: I can drink coffee", "why hasn't my starter risen yet" are not about the world now.
_FIRST_PERSON = re.compile(r"\b(?:i|i'm|im|i've|ive|i'd|my|me|mine|we|our|us)\b", re.IGNORECASE)
_CLAUSE_EDGE = re.compile(r"[.?!;,]")


def current_status_cue(text: str) -> str | None:
    """The word asking about the present state of the world, if ``text`` has one.

    A cue counts only in a clause that is not about the user, so personal and
    hypothetical uses of the same words are left to the router.
    """

    for match in _CURRENT_STATUS.finditer(text or ""):
        edges = [edge.end() for edge in _CLAUSE_EDGE.finditer(text, 0, match.start())]
        start = edges[-1] if edges else 0
        after = _CLAUSE_EDGE.search(text, match.end())
        clause = text[start : after.start() if after else len(text)]
        if not _FIRST_PERSON.search(clause):
            return match.group(0)
    return None


def is_bare_search_command(query: str) -> bool:
    """Whether ``query`` only asks to search, without saying for what."""

    return bool(_BARE_SEARCH_COMMAND.match(query or ""))


_CURRENT_WEB_SIGNAL = re.compile(
    r"\b(?:latest|newest|current|currently|today|yesterday|tomorrow|recent news|"
    r"right now|this week|news|price|prices|cost|schedule|fixtures?|"
    r"ranking|rankings|version|available|availability)\b",
    re.IGNORECASE,
)
_LOCAL_DATETIME = re.compile(
    r"(?:"
    r"\b(?:what(?:'s| is)|tell me|show me)\s+(?:the\s+)?"
    r"(?:current\s+)?(?:date|time|day)(?:\s+(?:today|now))?\b|"
    r"\bwhat\s+date\s+is\s+it\b|"
    r"\bwhat\s+time\s+is\s+it\b|"
    r"\btoday(?:'s| is the)?\s+date\b"
    r")",
    re.IGNORECASE,
)
_WEATHER_SIGNAL = re.compile(
    r"\b(?:weather|forecast|temperature|how hot|how cold|rain(?:ing)?|snow(?:ing)?)\b",
    re.IGNORECASE,
)
_RELEASE_SIGNAL = re.compile(
    r"\b(?:release date|releas(?:e|ed|es|ing)|premier(?:e|ed|es|ing)|"
    r"coming out|launch(?: date|ed|es|ing)?)\b",
    re.IGNORECASE,
)
# The word "release" alone is not a request for a date. "What changed in the
# latest release of <software>?" is a changelog question, and routing it to the
# release-date pipeline made it demand a date that no source could supply.
_RELEASE_DATE_QUESTION = re.compile(
    r"(?:\bwhen\b|\bwhat date\b|\brelease date\b|\blaunch date\b|"
    r"\bpremiere date\b|\bcoming out\b|\bout yet\b|\bdrops?\b|"
    r"\bhow long until\b)",
    re.IGNORECASE,
)
# "What changed / what's new in X" can never be answered from training data, so
# it is an external-information request no matter what the model router thinks.
CHANGE_QUESTION = re.compile(
    r"(?:\bwhat(?:\'s|s)?\s+(?:has\s+)?(?:recently\s+)?(?:changed|new)\b|"
    r"\bwhat\s+changed\b|\bchangelog\b|\brelease notes\b|"
    r"\bnew\s+(?:features?|changes?)\s+in\b)",
    re.IGNORECASE,
)
_CONNECTOR_SIGNAL = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:use|call|invoke|run|execute|query)\s+(?:the\s+)?"
    r"(?:[\w.-]+\s+)?(?:connector|mcp|api|tool)\b",
    re.IGNORECASE,
)

# These patterns identify declarations before generic freshness terms are considered.
# They intentionally cover common spelling variants from real user input.
_PERSONAL_DECLARATION = re.compile(
    r"^\s*(?:actually[,:\s]+|correction[,:\s]+|please\s+remember(?:\s+that)?\s+|"
    r"remember(?:\s+that)?\s+)?(?:"
    r"my\s+(?:name|age|occupation|job|location|country|nationality|education|"
    r"favo(?:u)?rite|preferred|goal|project|projects)\b|"
    r"call\s+me\b|i\s+go\s+by\b|"
    r"i(?:\s+am|'m)\s+\d{1,3}\s+years?\s+old\b|i\s+turned\s+\d{1,3}\b|"
    r"i\s+(?:recently\s+)?graduated\s+from\b|"
    r"i\s+(?:studied|attended|completed|earned)\b|"
    r"i(?:\s+am|'m|have\s+been)\s+(?:currently\s+)?(?:based|located)\s+in\b|"
    r"i(?:\s+currently)?\s+live\s+in\b|i(?:\s+am|'m)\s+from\b|"
    r"i\s+moved\s+to\b|i\s+work\s+as\b|"
    r"i(?:\s+am|'m)\s+(?:a|an)\b|"
    r"i(?:\s+am|'m)?\s+(?:(?:currently|now)\s+)?"
    r"(?:building|developing|creating|working\s+on|playing|reading|watching)\b|"
    r"i\s+(?:prefer|prioriti[sz]e|like|love+|hate|dislike|use)\b|"
    r"i\s+find\b|"
    r"i\s+(?:plan|intend|aim|hope)\s+to\b|"
    r"i\s+(?:mainly\s+)?want\s+to\b"
    r")",
    re.IGNORECASE,
)
_PERSONAL_RECALL = re.compile(
    r"\b(?:who am i|what(?:'s| is) my (?:current\s+)?"
    r"(?:name|age|location|occupation|job|education|country|nationality)|"
    r"how old am i|where (?:am i|do i live)|"
    r"what do you know about me|tell me about me|my profile|"
    r"(?:what|show|list|explain|remind me of|tell me about)\s+"
    r"(?:are\s+)?my\s+(?:current\s+|active\s+|latest\s+)?"
    r"(?:goals|projects|preferences|interests|favorites|favourites)|"
    r"what (?:projects )?am i (?:building|working on)|"
    r"what am i working on|what do i (?:prefer|like|use))\b",
    re.IGNORECASE,
)
_META_LANGUAGE = re.compile(
    r"\b(?:what does|define|meaning of|definition of|explain)\s+"
    r"(?:the\s+)?(?:word|term|phrase|keyword)\b",
    re.IGNORECASE,
)

_CURRENCY_CODES = {
    "dollar": "USD",
    "dollars": "USD",
    "usd": "USD",
    "rupee": "INR",
    "rupees": "INR",
    "inr": "INR",
    "euro": "EUR",
    "euros": "EUR",
    "eur": "EUR",
    "pound": "GBP",
    "pounds": "GBP",
    "gbp": "GBP",
    "yen": "JPY",
    "jpy": "JPY",
}
_CURRENCY_TOKEN = r"(?:USD|INR|EUR|GBP|JPY|dollars?|rupees?|euros?|pounds?|yen)"

#: The capabilities the router may choose between, each with the one line the
#: router is told about it. A route whose capability is disabled is left out of
#: the prompt entirely, so the router can only choose what can actually run.
_ROUTE_LINES: dict[str, str] = {
    "direct": (
        "- direct: answer from general knowledge and the conversation: explanations, "
        "reasoning, math, writing, coding, advice, opinions, brainstorming and casual talk."
    ),
    "web": "- web: retrieve sources from the web.",
    "memory": (
        "- memory: the user asks about their own saved information, things they said "
        "earlier, their preferences, goals or past chats."
    ),
    "calendar": (
        "- calendar: the user wants to see, add, change or remove events or reminders in "
        "their own schedule, or states a specific upcoming personal commitment with a day "
        "or time."
    ),
    "weather": "- weather: the user wants current or forecast weather conditions for a place.",
}
ROUTER_ROUTES: tuple[str, ...] = tuple(_ROUTE_LINES)

#: Schematic examples: placeholders stand for whatever the user actually says.
#: Concrete prompts live in the tests, never here, so the router learns the
#: criteria rather than a handful of cases.
_ROUTE_EXAMPLES: dict[str, str] = {
    "web": (
        "USER: <asks for a value that changes over time>\n"
        'JSON: {"route":"web","query":"<the thing> <the value>","place":""}\n'
        "USER: <says they keep seeing some name or phrase everywhere and asks what it is>\n"
        'JSON: {"route":"web","query":"<the exact name or phrase>","place":""}\n'
        "USER: <asks what is going on with something lately>\n"
        'JSON: {"route":"web","query":"<the thing> latest news","place":""}'
    ),
    "direct": (
        "USER: <asks to explain a well-established concept>\n"
        'JSON: {"route":"direct","query":"","place":""}'
    ),
    "calendar": (
        "USER: <asks to be reminded of something at a stated time>\n"
        'JSON: {"route":"calendar","query":"","place":""}'
    ),
    "memory": (
        "USER: <asks what they told Neo earlier about themselves>\n"
        'JSON: {"route":"memory","query":"","place":""}'
    ),
    "weather": (
        "USER: <asks about conditions outside in a named place>\n"
        'JSON: {"route":"weather","query":"","place":"<the place>"}'
    ),
}


def route_decision_prompt(
    routes: tuple[str, ...] | list[str] = ROUTER_ROUTES, *, today: str | None = None
) -> str:
    """The router's system prompt, listing only the routes that can run.

    The router decides from what an accurate answer requires. It is not asked
    for a confidence value: nothing consumed one, and a small model's
    self-reported confidence is not calibrated, so it only added output tokens.
    """

    enabled = [route for route in ROUTER_ROUTES if route in set(routes) or route == "direct"]
    choices = "|".join(enabled)
    return (
        "You are Neo's request router. Choose the capability that should handle the "
        "user's latest message. You get the message and, when present, a short excerpt of "
        "the conversation before it.\n"
        f"Today's date is {today or today_for_prompt()}. Interpret years relative to this date.\n\n"
        "Return exactly one JSON object and nothing else:\n"
        f'{{"route": "{choices}", "query": "<standalone web search query, only when route '
        'is web, otherwise empty>", "place": "<the place named for weather, only when route '
        'is weather, otherwise empty>"}\n\n'
        "Routes:\n" + "\n".join(_ROUTE_LINES[route] for route in enabled) + "\n\n"
        "Decide from what an accurate answer requires:\n"
        "- If it depends on information that is live, current, local, niche, or likely to "
        "have changed since you were trained, choose the capability that provides it.\n"
        "- If sources would materially improve factual correctness, choose web.\n"
        "- If the user says they keep seeing or hearing about something, or calls it new, "
        "trending, viral or recent, they want current information about a specific thing: "
        "choose web.\n"
        "- A name or phrase you do not recognise, or one that could be a recent product, "
        "tool, project, title or event, needs web.\n"
        "- Asking what is up, going on, new or happening with something asks for its latest "
        "news, releases and plans, not its background or history: choose web.\n"
        "- Asking whether something the user saw or heard about is real, current or common "
        "needs web.\n"
        "- If any part of the message needs current information (whether something exists "
        "or is in use yet, its latest status, recent approvals or releases), choose web, even "
        "when the rest asks for an explanation.\n"
        "- If it can be answered reliably from general knowledge or the conversation, "
        "choose direct. Explaining a well-established concept stays direct.\n"
        "- Whether the request is an action or a question matters: actions on the user's "
        "own schedule are calendar.\n"
        "- Your own uncertainty is not a reason to choose or to avoid any route.\n"
        "- Never choose a route because of a single topic word.\n\n"
        'Use the excerpt to resolve references such as "it", "that one", "what '
        'about ..." and short agreements; agreeing to an offer to search means web, about '
        "the earlier question. A follow-up continues the earlier request: after a question "
        'about the latest releases, news or developments of one thing, "what about" '
        "another thing asks the same about it. For web, write a query that stands alone. "
        "Keep the exact name or phrase the user uses for the thing they ask about, even when "
        "it looks like ordinary words, fixing only obvious typos; keep numbers exactly; and "
        "include no personal details unless the user asked to search for them.\n\n"
        "Examples (placeholders stand for whatever the user actually says):\n"
        + "\n".join(_ROUTE_EXAMPLES[route] for route in enabled if route in _ROUTE_EXAMPLES)
        + "\n"
    )


_ROUTE_DECISION_JSON = re.compile(r"\{\s*\"route\".*?\}", re.DOTALL)


def _parse_route_decision(
    llm: object, raw: str, enabled: tuple[str, ...]
) -> tuple[str, str, str] | None:
    """``(route, query, place)`` from the router's reply, or ``None`` if unusable."""

    cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
    match = _ROUTE_DECISION_JSON.search(cleaned or "")
    try:
        payload = json.loads(match.group(0) if match else (cleaned or "").strip())
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    route = str(payload.get("route") or "").strip().lower()
    if route not in enabled:
        return None
    query = payload.get("query")
    place = payload.get("place")
    return (
        route,
        query.strip() if isinstance(query, str) else "",
        place.strip() if isinstance(place, str) else "",
    )


_WEATHER_LOCATION_SYSTEM_PROMPT = """You are Neo's conservative place-name checker.

A phrase was pulled out of a chat message as a possible location for a weather
lookup. Decide whether it actually names a real-world place (a city, region,
country, or other named geographic location) -- not a greeting, acknowledgement,
instruction, opinion, or unrelated sentence fragment.

Return exactly one JSON object with no Markdown or extra text:
{"location": "<place name>"} if the phrase clearly names a real place, or
{"location": null} if it does not, or you are unsure.

Never guess. When the phrase could plausibly be something other than a place,
return null.

Examples (placeholders stand for whatever the phrase actually is):
PHRASE: hi
JSON: {"location": null}
PHRASE: yeah got it
JSON: {"location": null}
PHRASE: stop using
JSON: {"location": null}
PHRASE: thanks
JSON: {"location": null}
PHRASE: I currently use a <device>
JSON: {"location": null}
PHRASE: <a city name>
JSON: {"location": "<the city name>"}
PHRASE: what about <a city name>
JSON: {"location": "<the city name>"}
PHRASE: <a multi-word place name in lowercase>
JSON: {"location": "<the place name, capitalised>"}
"""

_WEATHER_LOCATION_JSON = re.compile(r"\{\s*\"location\".*?\}", re.DOTALL)


class SearchIntentResolver:
    """Resolve live-data intent conservatively.

    Topic similarity alone never selects a live operation. Personal declarations
    are suppressed before freshness and feature-name matching.
    """

    #: What the last ``route_with_model`` call did: ``{"status": "ok" | "failed" |
    #: "skipped", ...}``. Read by the chat service for diagnostics.
    last_router: dict[str, object] | None = None

    def resolve(
        self,
        query: str,
        *,
        previous: ResolvedSearchIntent | None = None,
        llm: LLMClient | None = None,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> ResolvedSearchIntent:
        original = query
        cleaned = _SPACE.sub(" ", query).strip()

        if not cleaned:
            return self._result(
                SearchIntentKind.NONE,
                original,
                cleaned,
                "Empty query.",
                1.0,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        if _EXPLICIT_REMEMBER_COMMAND.search(cleaned):
            return self._result(
                SearchIntentKind.NONE,
                original,
                cleaned,
                "Explicit remember command; keep local and offer it to memory.",
                0.99,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )
        if self._is_personal_declaration(cleaned):
            return self._result(
                SearchIntentKind.NONE,
                original,
                cleaned,
                "Personal declaration; keep local and offer it to memory.",
                0.99,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )
        if _PERSONAL_RECALL.search(cleaned):
            return self._result(
                SearchIntentKind.NONE,
                original,
                cleaned,
                "Personal recall query; use local memory.",
                0.99,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )
        if _META_LANGUAGE.search(cleaned):
            return self._result(
                SearchIntentKind.NONE,
                original,
                cleaned,
                "Meta-language explanation does not require live data.",
                0.98,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        if _CONNECTOR_SIGNAL.search(cleaned):
            return self._result(
                SearchIntentKind.CONNECTOR_TOOL,
                original,
                cleaned,
                "Explicit connector/tool invocation.",
                0.92,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        if _LOCAL_DATETIME.search(cleaned) and not _RELEASE_SIGNAL.search(cleaned):
            return self._result(
                SearchIntentKind.LOCAL_DATETIME,
                original,
                cleaned,
                "Current date/time can be resolved from a validated local timezone.",
                0.99,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        weather = self._weather_intent(cleaned, previous, llm)
        if weather is not None:
            return weather.model_copy(
                update={
                    "original_query": original,
                    "timezone": timezone,
                    "locale": locale,
                }
            )

        currency = self._currency_intent(cleaned, previous)
        if currency is not None:
            return currency.model_copy(
                update={
                    "original_query": original,
                    "timezone": timezone,
                    "locale": locale,
                }
            )

        release = self._release_intent(cleaned, previous)
        if release is not None:
            return release.model_copy(
                update={
                    "original_query": original,
                    "timezone": timezone,
                    "locale": locale,
                }
            )

        if _EXPLICIT_LOOKUP.search(cleaned):
            return self._result(
                SearchIntentKind.GENERAL_WEB,
                original,
                cleaned,
                "Explicit online lookup request.",
                0.99,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        if CHANGE_QUESTION.search(cleaned) and _CURRENT_WEB_SIGNAL.search(cleaned):
            return self._result(
                SearchIntentKind.GENERAL_WEB,
                original,
                cleaned,
                "Asks what changed in something current; needs external sources.",
                0.95,
                timezone=timezone,
                locale=locale,
                decision_source="structured",
            )

        return self._result(
            SearchIntentKind.NONE,
            original,
            cleaned,
            "No clear live-data request.",
            0.8,
            timezone=timezone,
            locale=locale,
            decision_source="fallback",
        )

    def resolve_explicit(
        self,
        query: str,
        *,
        previous: ResolvedSearchIntent | None = None,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> ResolvedSearchIntent | None:
        """Stage A: requests that are unambiguous without asking a model.

        Only operations whose wording fully specifies them: the current local date
        or time, a currency conversion (or a follow-up to one), a release-date
        question, an explicit connector command, or an explicit instruction to
        search the web for something. Everything else returns ``None`` and is
        decided by ``route_with_model``. No model is called here.
        """

        cleaned = _SPACE.sub(" ", query or "").strip()
        if not cleaned:
            return None
        # A rule can only call a request unambiguous when the request is all the
        # message contains. Pasted material -- a document, a log, code -- can hold
        # any of these phrases ("released on", "convert", "what time") without the
        # user asking for that operation, so such messages go to the router.
        if not _is_single_request(query):
            return None
        context = {"timezone": timezone, "locale": locale}
        if _CONNECTOR_SIGNAL.search(cleaned):
            return self._result(
                SearchIntentKind.CONNECTOR_TOOL,
                query,
                cleaned,
                "Explicit connector/tool invocation.",
                0.99,
                decision_source="structured",
                route="connector",
                **context,
            )
        if _LOCAL_DATETIME.search(cleaned) and not _RELEASE_SIGNAL.search(cleaned):
            return self._result(
                SearchIntentKind.LOCAL_DATETIME,
                query,
                cleaned,
                "Current date/time can be resolved from a validated local timezone.",
                0.99,
                decision_source="structured",
                route="time",
                **context,
            )
        currency = self._currency_intent(cleaned, previous)
        if currency is not None:
            return currency.model_copy(
                update={
                    "original_query": query,
                    "decision_source": "structured",
                    "route": "currency",
                    **context,
                }
            )
        release = self._release_intent(cleaned, previous)
        if release is not None:
            return release.model_copy(
                update={
                    "original_query": query,
                    "decision_source": "structured",
                    "route": "release_date",
                    **context,
                }
            )
        if _EXPLICIT_WEB_COMMAND.search(cleaned) and not is_bare_search_command(cleaned):
            return self._result(
                SearchIntentKind.GENERAL_WEB,
                query,
                cleaned,
                "Explicit instruction to search the web.",
                0.99,
                decision_source="structured",
                route="web",
                **context,
            )
        return None

    def route_with_model(
        self,
        query: str,
        *,
        llm: LLMClient | None,
        context: str | None = None,
        previous: ResolvedSearchIntent | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        routes: tuple[str, ...] | list[str] = ROUTER_ROUTES,
        query_label: Label | None = None,
    ) -> ResolvedSearchIntent:
        """Stage B: one fast, non-reasoning model call chooses the capability.

        The same prompt, inputs and settings are used whatever effort the turn
        has, so Low and High always route identically. A failure -- no client,
        a provider error, unparseable output, or output cut off at its limit --
        is recorded in ``last_router`` and falls back to a direct answer; an
        explicit request never depends on this, because Stage A already took it.
        """

        input_label = label_of(query_label)
        if context or previous is not None:
            input_label = join(input_label, Label.private("chat_private"))
        cleaned = _SPACE.sub(" ", query or "").strip()
        base = self._result(
            SearchIntentKind.NONE,
            query,
            cleaned,
            "Router unavailable; answering directly.",
            _FALLBACK_CONFIDENCE,
            source_label_json=to_json(input_label),
            timezone=timezone,
            locale=locale,
            decision_source="fallback",
            route="direct",
        )
        self.last_router = {"status": "skipped"}
        if llm is None or not cleaned:
            return base
        enabled = tuple(
            route for route in ROUTER_ROUTES if route in set(routes) or route == "direct"
        )
        user_content = (
            f"CONVERSATION EXCERPT:\n{context.strip()}\n\nLATEST MESSAGE: {cleaned}"
            if context and context.strip()
            else f"LATEST MESSAGE: {cleaned}"
        )
        messages = [
            LLMMessage(
                role="system",
                content=route_decision_prompt(enabled, today=today_for_prompt(timezone)),
                source_label=public("builtin:route_decision"),
            ),
            LLMMessage(role="user", content=user_content, source_label=input_label),
        ]
        finish_reason: str | None = None
        try:
            with_metadata = getattr(llm, "chat_with_metadata", None)
            if callable(with_metadata):
                result = with_metadata(messages, temperature=0.0)
                raw = str(getattr(result, "content", "") or "")
                finish_reason = getattr(result, "finish_reason", None)
            else:
                raw = llm.chat(messages, temperature=0.0)
        except Exception as exc:  # provider errors, timeouts
            self.last_router = {
                "status": "failed",
                "cause": f"provider_error: {type(exc).__name__}",
            }
            _LOG.warning("router_failed cause=provider_error error=%s", exc)
            return base
        decision = _parse_route_decision(llm, raw, enabled)
        if decision is None:
            cause = "length" if finish_reason == "length" else "unparseable"
            self.last_router = {"status": "failed", "cause": cause, "finish_reason": finish_reason}
            _LOG.warning("router_failed cause=%s finish_reason=%s", cause, finish_reason)
            return base
        route, search_query, place = decision
        self.last_router = {"status": "ok", "route": route, "finish_reason": finish_reason}
        common = {
            "source_label_json": to_json(input_label),
            "timezone": timezone,
            "locale": locale,
            "decision_source": "model",
            "route": route,
        }
        if route == "web":
            return self._result(
                SearchIntentKind.GENERAL_WEB,
                query,
                search_query or cleaned,
                "Router: the answer needs sources from the web.",
                _MODEL_CONFIDENCE,
                **common,
            )
        if route == "calendar":
            return self._result(
                SearchIntentKind.CALENDAR,
                query,
                cleaned,
                "Router: the request concerns the user's own schedule.",
                _MODEL_CONFIDENCE,
                **common,
            )
        if route == "weather":
            weather = self._weather_intent(
                cleaned, previous, llm, confirmed=True, place=place or None
            )
            if weather is not None:
                return weather.model_copy(
                    update={"original_query": query, "confidence": _MODEL_CONFIDENCE, **common}
                )
            return self._result(
                SearchIntentKind.WEATHER,
                query,
                cleaned,
                "Router: weather, but no place was named.",
                _MODEL_CONFIDENCE,
                **common,
            )
        # Only a message that is one short request: pasted code or a document may use the
        # same words ("currently", "latest") without asking about the world.
        if route == "direct" and _is_single_request(query):
            cue = current_status_cue(cleaned)
            if cue is not None:
                # A message that also asks how things stand now ("... and whether any cars
                # use them yet") cannot be answered from training data, however much of it
                # is explanation; a small router often lets the explanation decide.
                self.last_router = {**self.last_router, "current_cue": cue}
                return self._result(
                    SearchIntentKind.GENERAL_WEB,
                    query,
                    cleaned,
                    f"Asks how things stand now ({cue!r}); the answer needs sources.",
                    _MODEL_CONFIDENCE,
                    **{**common, "route": "web"},
                )
        reason = (
            "Router: the user asks about their own saved information."
            if route == "memory"
            else "Router: answerable from general knowledge and the conversation."
        )
        return self._result(
            SearchIntentKind.NONE, query, cleaned, reason, _MODEL_CONFIDENCE, **common
        )

    @staticmethod
    def _model_confirm_location(fragment: str, llm: LLMClient | None) -> str | None:
        """Ask the selected chat model whether ``fragment`` names a real place.

        Mirrors ``_model_route``'s conservative-by-default contract: a missing
        client, provider error, or an unparseable/ambiguous/wrongly-typed
        reply all resolve to ``None`` (reject) -- never to a guessed location.
        """
        if llm is None or not fragment.strip():
            return None
        try:
            raw = llm.chat(
                [
                    LLMMessage(role="system", content=_WEATHER_LOCATION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=f"PHRASE: {fragment.strip()}"),
                ],
                temperature=0.0,
            )
            cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
            match = _WEATHER_LOCATION_JSON.search(cleaned)
            payload = json.loads(match.group(0) if match else cleaned.strip())
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        location = payload.get("location")
        if not isinstance(location, str):
            return None
        location = location.strip()
        return location or None

    def _is_personal_declaration(self, query: str) -> bool:
        match = _PERSONAL_DECLARATION.search(query)
        if match is None:
            return False

        # "I want to find today's weather" is an information request, not a goal.
        if re.match(
            r"^\s*i\s+want\s+to\s+(?:find|know|check|search|look\s+up|see)\b",
            query,
            re.IGNORECASE,
        ) and (_CURRENT_WEB_SIGNAL.search(query) or _WEATHER_SIGNAL.search(query)):
            return False

        remainder = query[match.end() :]
        explicit_follow_up = re.search(
            r"(?:[?,;]|\b(?:but|then|also)\b).*\b"
            r"(?:who|what|when|where|why|how|search|look\s+up|verify|find)\b",
            remainder,
            re.IGNORECASE,
        )
        return explicit_follow_up is None

    def _currency_intent(
        self, query: str, previous: ResolvedSearchIntent | None
    ) -> ResolvedSearchIntent | None:
        tokens = list(re.finditer(_CURRENCY_TOKEN, query, re.IGNORECASE))
        prior_is_currency = previous is not None and previous.kind == SearchIntentKind.CURRENCY
        if (
            prior_is_currency
            and not tokens
            and not re.search(
                r"\b(?:again|convert|conversion|currency|exchange|rate|same|"
                r"what about|\d+(?:\.\d+)?)\b",
                query,
                re.IGNORECASE,
            )
        ):
            return None
        has_currency_language = bool(
            re.search(
                r"\b(?:convert|conversion|exchange rate|worth|how much|currency)\b",
                query,
                re.IGNORECASE,
            )
        )
        if len(tokens) < 2 and not prior_is_currency:
            return None
        if not has_currency_language and not prior_is_currency:
            return None

        amount_match = re.search(
            rf"(?P<amount>\d+(?:\.\d+)?)\s*(?P<currency>{_CURRENCY_TOKEN})\b",
            query,
            re.IGNORECASE,
        )
        amount = (
            self._decimal(amount_match.group("amount"))
            if amount_match
            else (
                previous.amount
                if prior_is_currency and previous is not None and previous.amount is not None
                else Decimal("1")
            )
        )

        currencies = [self._currency_code(match.group(0)) for match in tokens]
        from_currency = currencies[0] if currencies else None
        to_currency = currencies[1] if len(currencies) > 1 else None
        if prior_is_currency:
            from_currency = from_currency or previous.from_currency
            to_currency = to_currency or previous.to_currency
            if len(currencies) == 1 and currencies[0] == previous.from_currency:
                to_currency = previous.to_currency
            elif len(currencies) == 1 and currencies[0] == previous.to_currency:
                from_currency = previous.from_currency
                to_currency = currencies[0]

        if from_currency is None or to_currency is None or from_currency == to_currency:
            return None
        resolved = f"Convert {amount} {from_currency} to {to_currency}"
        return self._result(
            SearchIntentKind.CURRENCY,
            query,
            resolved,
            "Explicit currency conversion or contextual conversion follow-up.",
            0.98,
            amount=amount,
            from_currency=from_currency,
            to_currency=to_currency,
        )

    def _weather_intent(
        self,
        query: str,
        previous: ResolvedSearchIntent | None,
        llm: LLMClient | None = None,
        *,
        confirmed: bool = False,
        place: str | None = None,
    ) -> ResolvedSearchIntent | None:
        """Find the place and day for a weather request.

        ``confirmed`` means the router already decided this is a weather request,
        so the weather-word check is skipped and only parameters are extracted;
        ``place`` is the place the router read from the message, which needs no
        second confirmation.
        """

        prior_is_weather = previous is not None and previous.kind == SearchIntentKind.WEATHER
        explicit = confirmed or _WEATHER_SIGNAL.search(query) is not None
        if not explicit and not prior_is_weather:
            return None

        location: str | None = place.strip(" .,?!") if place and place.strip() else None
        matched_pattern_index: int | None = None
        for index, pattern in enumerate(
            (
                r"\b(?:weather|forecast|temperature)\s+(?:in|for|at)\s+"
                r"(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)(?:\s+(?:today|now|tomorrow))?[?.!]*$",
                r"^(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)\s+"
                r"(?:weather|forecast|temperature)(?:\s+(?:today|now|tomorrow))?[?.!]*$",
                r"\bi\s+(?:live|am based|am located)\s+in\s+"
                r"(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)(?:[,;]|$)",
            )
        ):
            if location is not None:
                break
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                location = match.group("location").strip(" .,?!")
                matched_pattern_index = index
                break
        if (
            location
            and matched_pattern_index is not None
            and re.search(
                r"\b(?:i|you|want|find|check|show|tell|what|when|where|how|today's)\b",
                location,
                re.IGNORECASE,
            )
        ):
            location = None

        # Pattern index 1 ("<text> weather") accepts arbitrary leading text as
        # a location -- e.g. "stop using weather" -> "stop using". The regex
        # has no way to know whether that leading text names a real place, so
        # a cheap, deterministic model check confirms the guess before it is
        # trusted. A rejection must not silently fall back to "same place as
        # before" either (see `pattern2_rejected` below) -- otherwise "stop
        # using weather" right after a query about one city would resolve to
        # that city's weather instead of being recognised as unrelated.
        pattern2_rejected = False
        if matched_pattern_index == 1 and location:
            confirmed = self._model_confirm_location(location, llm)
            if confirmed is None:
                pattern2_rejected = True
            location = confirmed

        if location is None and not explicit and prior_is_weather:
            fragment = re.sub(
                r"\b(?:today|now|tomorrow|please|what about|and|try again|again|same)\b",
                " ",
                query,
                flags=re.IGNORECASE,
            )
            fragment = _SPACE.sub(" ", fragment).strip(" .,?!")
            if not fragment:
                # The whole message was filler/date words ("and tomorrow?"),
                # so this is a pure date continuation of the same question.
                location = previous.location
            else:
                # A genuine "for where?" follow-up answer is a short place
                # name, not a new full sentence. This length/punctuation
                # bound is only a cheap pre-filter -- "hi" and "yeah got it"
                # both pass it trivially -- so a plausible-looking fragment is
                # additionally confirmed by the model before it is trusted as
                # a location. A rejected or unconfirmable fragment falls
                # through to `return None` below instead of resolving to
                # WEATHER at all, which is what stops a single bad guess from
                # re-arming this branch on the next turn: a non-WEATHER
                # intent cannot make the next turn's `prior_is_weather` true.
                looks_like_place = (
                    len(fragment.split()) <= 5
                    and not _QUESTION_WORD.search(fragment)
                    and not re.search(r"[.!?;:]", query)
                )
                if looks_like_place:
                    location = self._model_confirm_location(fragment, llm)
        if location is None and prior_is_weather and explicit and not pattern2_rejected:
            location = previous.location
        if not location and not explicit:
            return None

        date = "tomorrow" if re.search(r"\btomorrow\b", query, re.IGNORECASE) else "today"
        resolved = f"Weather in {location} {date}" if location else query
        return self._result(
            SearchIntentKind.WEATHER,
            query,
            resolved,
            "Explicit weather request or contextual weather follow-up.",
            0.97,
            location=location,
            date=date,
        )

    def _release_intent(
        self, query: str, previous: ResolvedSearchIntent | None
    ) -> ResolvedSearchIntent | None:
        if _RELEASE_SIGNAL.search(query) is None:
            return None
        if _RELEASE_DATE_QUESTION.search(query) is None:
            return None

        entity = self._release_entity(query)
        declared_entity = re.search(
            r"\bi(?:\s+am|'m)\s+(?:currently\s+)?"
            r"(?:playing|watching|reading)\s+"
            r"(?P<entity>[^,;?!]{2,100})",
            query,
            re.IGNORECASE,
        )
        if declared_entity is not None:
            entity = declared_entity.group("entity").strip()
        if (
            (not entity or entity.lower() in {"it", "this", "that"})
            and previous is not None
            and previous.kind == SearchIntentKind.RELEASE_DATE
        ):
            entity = previous.entity
        if not entity or entity.lower() in {"it", "this", "that"}:
            return None
        # A place named at the end of the question ("... in <Place>?"), whatever
        # it is: capitalised words after "in" or "for", not a list of countries.
        region_match = re.search(
            r"\b(?:in|for)\s+(?P<region>[A-Z][\w.-]*(?:\s+[A-Z][\w.-]*){0,2})\s*[?.!]*\s*$",
            query,
        )
        region = region_match.group("region") if region_match else None
        if (
            region is None
            and previous is not None
            and previous.kind == SearchIntentKind.RELEASE_DATE
        ):
            region = previous.region
        resolved = f"{entity} {region + ' ' if region else ''}release date official"
        return self._result(
            SearchIntentKind.RELEASE_DATE,
            query,
            resolved.strip(),
            "Explicit release-date request.",
            0.95,
            entity=entity,
            region=region,
        )

    @staticmethod
    def _release_entity(query: str) -> str | None:
        cleaned = re.sub(
            r"^\s*(?:when|what date)\s+(?:is|will|does|did)?\s*",
            "",
            query,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(
            r"\b(?:going to|set to|scheduled to|will|does|did|is)?\s*"
            r"(?:release|released|releasing|premiere|coming out|launch)\b.*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" .,?!")
        cleaned = re.sub(r"^(?:the\s+)?", "", cleaned, flags=re.IGNORECASE)
        if re.match(r"^(?:what|who|how|why|which)\b", cleaned, flags=re.IGNORECASE):
            return None
        return cleaned or None

    @staticmethod
    def _currency_code(value: str) -> str:
        return _CURRENCY_CODES[value.lower()]

    @staticmethod
    def _decimal(value: str) -> Decimal:
        try:
            return Decimal(value)
        except InvalidOperation:
            return Decimal("1")

    @staticmethod
    def _result(
        kind: SearchIntentKind,
        original_query: str,
        resolved_query: str,
        reason: str,
        confidence: float,
        **kwargs: object,
    ) -> ResolvedSearchIntent:
        return ResolvedSearchIntent(
            kind=kind,
            original_query=original_query,
            resolved_query=resolved_query,
            reason=reason,
            confidence=confidence,
            **kwargs,
        )


def resolve_search_intent(
    query: str,
    *,
    previous: ResolvedSearchIntent | None = None,
    timezone: str | None = None,
    locale: str | None = None,
) -> ResolvedSearchIntent:
    return SearchIntentResolver().resolve(
        query,
        previous=previous,
        timezone=timezone,
        locale=locale,
    )
