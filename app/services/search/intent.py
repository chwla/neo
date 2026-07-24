from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from app.services.llm import LLMMessage
from app.services.search.types import ResolvedSearchIntent, SearchIntentKind

if TYPE_CHECKING:
    from app.services.llm import LLMClient

_SPACE = re.compile(r"\s+")
_QUESTION_WORD = re.compile(r"\b(?:who|what|when|where|why|how|which)\b", re.IGNORECASE)
_EXPLICIT_LOOKUP = re.compile(
    r"(?:^\s*(?:please\s+)?(?:search|find|google)\b|"
    r"\b(?:look\s+up|lookup|verify|fact[- ]?check|check online)\b)",
    re.IGNORECASE,
)
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
_CONNECTOR_SIGNAL = re.compile(
    r"^\s*(?:(?:can|could|would)\s+you\s+|please\s+)?"
    r"(?:use|call|invoke|run|execute|query)\s+(?:the\s+)?"
    r"(?:[\w.-]+\s+)?(?:connector|mcp|api|tool)\b",
    re.IGNORECASE,
)

# These patterns identify declarations before generic freshness terms are considered.
# They intentionally cover common spelling variants from real user input.
_PERSONAL_DECLARATION = re.compile(
    r"^\s*(?:actually[,:\s]+|please\s+remember(?:\s+that)?\s+|"
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
    r"i\s+want\s+to\b"
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

_ROUTE_DECISION_SYSTEM_PROMPT = """You are Neo's conservative chat router.
Decide how to answer a user message.

Return exactly one JSON object with no Markdown or extra text:
{"route":"memory"|"web"|"direct", "confidence":0.0-1.0}

Routes:
- memory: the user asks about their own prior statements, saved profile,
  preferences, goals, projects, activities, or chats.
- web: the user explicitly asks to search/verify online, or needs current external
  information that cannot reliably come from chat or saved memory.
- direct: explanation, writing, reasoning, casual conversation, or any ambiguous
  request. Default to direct when uncertain.

Never select web from a topic word alone. Words such as current, currently,
latest, recovery, memory, research, files, projects, notes, tasks, agent, and
restart are not web commands by themselves.

Examples:
USER: What am I currently building?
JSON: {"route":"memory","confidence":0.99}
USER: What projects am I currently working on?
JSON: {"route":"memory","confidence":0.99}
USER: What are my saved goals for this year?
JSON: {"route":"memory","confidence":0.99}
USER: Explain recovery after an application restart.
JSON: {"route":"direct","confidence":0.98}
USER: How should a local-first assistant manage memory?
JSON: {"route":"direct","confidence":0.98}
USER: Search the web for Kanye West concert dates in 2026.
JSON: {"route":"web","confidence":0.99}
USER: What is the current price of NVIDIA stock?
JSON: {"route":"web","confidence":0.96}
USER: Is SQLite WAL safe for a local app?
JSON: {"route":"direct","confidence":0.82}
"""

_ROUTE_DECISION_JSON = re.compile(r"\{\s*\"route\".*?\}", re.DOTALL)


class SearchIntentResolver:
    """Resolve live-data intent conservatively.

    Topic similarity alone never selects a live operation. Personal declarations
    are suppressed before freshness and feature-name matching.
    """

    def resolve(
        self,
        query: str,
        *,
        previous: ResolvedSearchIntent | None = None,
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

        weather = self._weather_intent(cleaned, previous)
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

    def resolve_with_model(
        self,
        query: str,
        *,
        llm: LLMClient | None,
        previous: ResolvedSearchIntent | None = None,
        timezone: str | None = None,
        locale: str | None = None,
    ) -> ResolvedSearchIntent:
        """Use the selected chat model for ordinary route choice.

        Deterministic parsing remains responsible for structured, safety-sensitive
        operations (weather, currency, date/time, explicit tools and explicit web
        commands). All ordinary questions are deliberately model-routed so a single
        freshness word cannot force external search.
        """
        base = self.resolve(query, previous=previous, timezone=timezone, locale=locale)
        if base.kind is not SearchIntentKind.NONE or not base.original_query.strip():
            return base

        decision = self._model_route(query, llm)
        if decision is None:
            return base.model_copy(
                update={
                    "reason": "No reliable model route decision; defaulting to local chat.",
                    "confidence": 0.7,
                    "decision_source": "fallback",
                }
            )

        route, confidence = decision
        if route == "web":
            return base.model_copy(
                update={
                    "kind": SearchIntentKind.GENERAL_WEB,
                    "reason": "Selected model identified a current external-information request.",
                    "confidence": confidence,
                    "decision_source": "model",
                }
            )
        reason = (
            "Selected model identified a personal-memory recall request."
            if route == "memory"
            else "Selected model identified a normal local chat request."
        )
        return base.model_copy(
            update={
                "reason": reason,
                "confidence": confidence,
                "decision_source": "model",
            }
        )

    @staticmethod
    def _model_route(query: str, llm: LLMClient | None) -> tuple[str, float] | None:
        if llm is None:
            return None
        try:
            raw = llm.chat(
                [
                    LLMMessage(role="system", content=_ROUTE_DECISION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=query),
                ],
                temperature=0.0,
            )
            cleaned = llm.clean_response(raw) if hasattr(llm, "clean_response") else raw
            match = _ROUTE_DECISION_JSON.search(cleaned)
            payload = json.loads(match.group(0) if match else cleaned.strip())
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        route = str(payload.get("route", "")).strip().lower()
        if route not in {"memory", "web", "direct"}:
            return None
        try:
            confidence = float(payload.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        return route, min(1.0, max(0.0, confidence))

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
        self, query: str, previous: ResolvedSearchIntent | None
    ) -> ResolvedSearchIntent | None:
        prior_is_weather = previous is not None and previous.kind == SearchIntentKind.WEATHER
        explicit = _WEATHER_SIGNAL.search(query) is not None
        if not explicit and not prior_is_weather:
            return None

        location: str | None = None
        for pattern in (
            r"\b(?:weather|forecast|temperature)\s+(?:in|for|at)\s+"
            r"(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)(?:\s+(?:today|now|tomorrow))?[?.!]*$",
            r"^(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)\s+"
            r"(?:weather|forecast|temperature)(?:\s+(?:today|now|tomorrow))?[?.!]*$",
            r"\bi\s+(?:live|am based|am located)\s+in\s+"
            r"(?P<location>[A-Za-z][A-Za-z .'-]{1,60}?)(?:[,;]|$)",
        ):
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                location = match.group("location").strip(" .,?!")
                break
        if location and re.search(
            r"\b(?:i|you|want|find|check|show|tell|what|when|where|how|today's)\b",
            location,
            re.IGNORECASE,
        ):
            location = None
        if not explicit and prior_is_weather:
            fragment = re.sub(
                r"\b(?:today|now|tomorrow|please|what about|and|try again|again|same)\b",
                " ",
                query,
                flags=re.IGNORECASE,
            )
            fragment = _SPACE.sub(" ", fragment).strip(" .,?!")
            if fragment and not _QUESTION_WORD.search(fragment):
                location = fragment
        if location is None and prior_is_weather:
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
        region_match = re.search(
            r"\b(?:in|for)\s+(India|UK|US|USA|Canada|Australia)\b",
            query,
            re.I,
        )
        region = region_match.group(1) if region_match else None
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
