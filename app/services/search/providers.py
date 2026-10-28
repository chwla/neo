from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from base64 import urlsafe_b64decode
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlparse

import requests

from app.core.config import get_settings
from app.services.disclosure import (
    Destination,
    DisclosureBlocked,
    Label,
    Labeled,
    label_of,
    to_json,
)
from app.services.disclosure.egress import EgressTimeoutError, EgressTransportError
from app.services.disclosure.egress import request as http_request
from app.services.disclosure.gate import GrantStore
from app.services.disclosure.service_credentials import CredentialScopeError, ServiceCredential
from app.services.search.types import SearchResult, WebSearchResponse

PROVIDER_INFO = {
    # "searxng" is Neo's own instance, imported into this process by
    # app/services/search/searxng_embedded.py -- no container, no port.
    # "external_searxng" is the one you point at a URL you run yourself.
    "searxng": ("SearXNG (built in)", False, True),
    "external_searxng": ("External SearXNG", False, True),
    "tavily": ("Tavily", True, False),
    "brave": ("Brave Search", True, False),
    "duckduckgo": ("DuckDuckGo", False, False),
    "bing_html": ("Bing HTML", False, False),
    "serper": ("Serper", True, False),
    "wikipedia": ("Wikipedia", False, False),
    "disabled": ("Disabled", False, False),
}


#: Kinds of result a search can be narrowed to. ``web`` is the default for every provider.
SEARCH_VERTICALS = ("web", "news", "scholarly")
#: SearXNG groups its engines into categories; these are the ones each vertical asks for.
SEARXNG_CATEGORIES = {"web": "general", "news": "news", "scholarly": "science"}


#: The one origin each keyed provider's credential may be presented to.
BRAVE_ORIGIN = "https://api.search.brave.com"
TAVILY_ORIGIN = "https://api.tavily.com"
SERPER_ORIGIN = "https://google.serper.dev"


class WebSearchProvider(ABC):
    name: str
    source_label: Label = Label.unknown("unlabelled_search")
    #: Grants for one approved agent tool call, and nothing else. ``None`` on every
    #: provider the registry hands out by default; set only by the approved-call
    #: path, on a provider instance that does not outlive that call.
    grants: GrantStore | None = None

    def _http(
        self,
        method: str,
        url: str,
        *,
        timeout: float,
        credential: ServiceCredential | None = None,
        **fields,
    ):
        # The labelled envelope carries only the search itself. A configured key is a
        # separate capability bound to this provider's origin, attached by egress at
        # transmission; it never shares the query's label (see service_credentials).
        try:
            return http_request(
                Labeled({"method": method, "url": url, **fields}, self.source_label),
                Destination("web-search", engine=self.name),
                grants=self.grants,
                timeout=timeout,
                credential=credential,
            ).value
        except EgressTimeoutError:
            raise requests.Timeout("Search provider timed out.") from None
        except (DisclosureBlocked, CredentialScopeError, EgressTransportError):
            raise requests.RequestException(
                "Search request requires approval or its provider is unavailable."
            ) from None

    #: Verticals this provider can actually narrow to. Others are ignored and searched as web.
    verticals: frozenset[str] = frozenset({"web"})

    @abstractmethod
    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        raise NotImplementedError


class _DuckDuckGoHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        class_name = attr.get("class", "")
        if tag == "a" and "result__a" in class_name:
            self._finish_current()
            self._current = {
                "title": "",
                "url": self._clean_url(attr.get("href", "")),
                "snippet": "",
            }
            self._in_title = True
        elif self._current is not None and tag in {"a", "div"} and "result__snippet" in class_name:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if self._in_title and tag == "a":
            self._in_title = False
        if self._in_snippet and tag in {"a", "div"}:
            self._in_snippet = False
            self._finish_current()

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self._current["title"] = f"{self._current['title']} {cleaned}".strip()
        elif self._in_snippet:
            self._current["snippet"] = f"{self._current['snippet']} {cleaned}".strip()

    def _clean_url(self, href: str) -> str:
        if href.startswith("//duckduckgo.com/l/"):
            parsed = urlparse(f"https:{href}")
            uddg = parse_qs(parsed.query).get("uddg", [""])[0]
            return unquote(uddg)
        return href

    def close(self) -> None:
        super().close()
        self._finish_current()

    def _finish_current(self) -> None:
        if self._current and self._current.get("title") and self._current.get("url"):
            self.results.append(self._current)
        self._current = None
        self._in_title = False
        self._in_snippet = False


class _BingHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._in_result = False
        self._in_h2 = False
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attr = dict(attrs)
        class_name = attr.get("class", "")
        if tag == "li" and "b_algo" in class_name:
            self._current = {"title": "", "url": "", "snippet": ""}
            self._in_result = True
            return
        if not self._in_result or self._current is None:
            return
        if tag == "h2":
            self._in_h2 = True
        elif tag == "a" and self._in_h2 and not self._current["url"]:
            href = attr.get("href", "")
            if href.startswith(("http://", "https://")):
                self._current["url"] = self._clean_url(href)
                self._in_title = True
        elif tag == "p":
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag == "h2" and self._in_h2:
            self._in_h2 = False
        if tag == "p" and self._in_snippet:
            self._in_snippet = False
        if tag == "li" and self._in_result:
            if self._current and self._current["title"] and self._current["url"]:
                self.results.append(self._current)
            self._current = None
            self._in_result = False
            self._in_h2 = False
            self._in_title = False
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        cleaned = " ".join(data.split())
        if not cleaned:
            return
        if self._in_title:
            self._current["title"] = f"{self._current['title']} {cleaned}".strip()
        elif self._in_snippet:
            self._current["snippet"] = f"{self._current['snippet']} {cleaned}".strip()

    def _clean_url(self, href: str) -> str:
        parsed = urlparse(href)
        if "bing.com" not in parsed.netloc:
            return href
        encoded = parse_qs(parsed.query).get("u", [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
        if not encoded:
            return href
        padded = encoded + "=" * (-len(encoded) % 4)
        try:
            decoded = urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            return href
        return decoded if decoded.startswith(("http://", "https://")) else href


def _settings_key(provider: str) -> str | None:
    settings = get_settings()
    if provider == "tavily":
        return settings.tavily_api_key or settings.web_search_api_key
    if provider == "brave":
        return settings.brave_api_key or settings.web_search_api_key
    if provider == "serper":
        return settings.serper_api_key or settings.web_search_api_key
    return None


def normalize_searxng_instance(value: str) -> str:
    cleaned = (value or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("SearXNG instance URL must be an http or https URL.")
    return cleaned


def provider_available(provider: str) -> bool:
    provider = provider.lower().strip()
    info = PROVIDER_INFO.get(provider)
    if not info:
        return False
    if provider == "searxng":
        from app.services.search.searxng_embedded import source_available

        return source_available()
    if provider == "external_searxng":
        try:
            normalize_searxng_instance(get_settings().searxng_instance)
        except ValueError:
            return False
        return True
    return not info[1] or bool(_settings_key(provider))


class SearXNGSearchProvider(WebSearchProvider):
    name = "searxng"
    verticals = frozenset(SEARXNG_CATEGORIES)

    def __init__(
        self,
        instance_url: str | None = None,
        timeout_seconds: float | None = None,
        user_agent: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        settings = get_settings()
        self.instance_url = (instance_url or settings.searxng_instance).strip()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent
        self.name = provider_name or self.name

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        try:
            instance = normalize_searxng_instance(self.instance_url)
        except ValueError as exc:
            return WebSearchResponse(query=query, provider=self.name, error=str(exc))

        params: dict[str, object] = {
            "q": query,
            "format": "json",
            "language": "en",
            "safesearch": 1,
        }
        if time_filter in {"day", "week", "month", "year"}:
            params["time_range"] = "week" if time_filter == "day" else time_filter
        if vertical in SEARXNG_CATEGORIES:
            params["categories"] = SEARXNG_CATEGORIES[vertical]
        try:
            response = self._http("GET",
                f"{instance}/search",
                params=params,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout:
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error="Configured SearXNG endpoint is unavailable.",
            )
        except requests.ConnectionError:
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error="Configured SearXNG endpoint is unavailable.",
            )
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            return WebSearchResponse(
                query=query, provider=self.name, error=f"SearXNG returned HTTP {status}."
            )
        except (requests.RequestException, json.JSONDecodeError) as exc:
            return WebSearchResponse(
                query=query, provider=self.name, error=f"SearXNG search failed: {exc}"
            )

        raw = [
            {
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or item.get("snippet") or "",
                "published_date": item.get("publishedDate") or item.get("published_date"),
            }
            for item in payload.get("results", [])
        ]
        return _results_response(
            query, self.name, raw, max_results, source_label=self.source_label,
        )


class DuckDuckGoSearchProvider(WebSearchProvider):
    name = "duckduckgo"

    def __init__(self, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        try:
            response = self._http("GET",
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout:
            return WebSearchResponse(
                query=query, provider=self.name, error="Search provider timed out."
            )
        except requests.RequestException as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")

        parser = _DuckDuckGoHTMLParser()
        parser.feed(response.text)
        return _results_response(
            query, self.name, parser.results, max_results, source_label=self.source_label,
        )


class BingHTMLSearchProvider(WebSearchProvider):
    name = "bing_html"

    def __init__(self, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        try:
            response = self._http("GET",
                "https://www.bing.com/search",
                params={"q": query},
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except requests.Timeout:
            return WebSearchResponse(
                query=query, provider=self.name, error="Search provider timed out."
            )
        except requests.RequestException as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")

        parser = _BingHTMLParser()
        parser.feed(response.text)
        return _results_response(
            query, self.name, parser.results, max_results, source_label=self.source_label,
        )


class BraveSearchProvider(WebSearchProvider):
    name = "brave"

    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        user_agent: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else _settings_key(self.name)
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        if not self.api_key:
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error="Brave Search requires WEB_SEARCH_API_KEY or BRAVE_API_KEY.",
            )
        params = {"q": query, "count": max_results}
        if time_filter in {"day", "week", "month", "year"}:
            params["freshness"] = time_filter
        try:
            response = self._http("GET",
                f"{BRAVE_ORIGIN}/res/v1/web/search",
                params=params,
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
                credential=ServiceCredential.header(
                    self.name, self.api_key, origin=BRAVE_ORIGIN, header="X-Subscription-Token"
                ),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")

        raw = [
            {
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                "snippet": item.get("description") or item.get("content") or "",
                "published_date": item.get("age") or item.get("page_age"),
            }
            for item in payload.get("web", {}).get("results", [])
        ]
        return _results_response(
            query, self.name, raw, max_results, source_label=self.source_label,
        )


class TavilySearchProvider(WebSearchProvider):
    name = "tavily"
    verticals = frozenset({"web", "news"})

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = _settings_key(self.name)
        self.timeout_seconds = settings.web_fetch_timeout_seconds

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        if not self.api_key:
            return WebSearchResponse(
                query=query, provider=self.name, error="Tavily requires TAVILY_API_KEY."
            )
        payload: dict[str, object] = {
            "query": query,
            "max_results": max_results,
            "include_answer": False,
        }
        if time_filter in {"day", "week", "month", "year"}:
            payload["days"] = {"day": 1, "week": 7, "month": 30, "year": 365}[time_filter]
        if vertical == "news":
            payload["topic"] = "news"
        try:
            response = self._http("POST",
                f"{TAVILY_ORIGIN}/search",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
                credential=ServiceCredential.header(
                    self.name,
                    self.api_key,
                    origin=TAVILY_ORIGIN,
                    header="Authorization",
                    template="Bearer {secret}",
                ),
            )
            response.raise_for_status()
            data = response.json()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                return WebSearchResponse(
                    query=query, provider=self.name, error="Tavily API key was rejected."
                )
            return WebSearchResponse(
                query=query,
                provider=self.name,
                error=(
                    "Tavily returned HTTP "
                    f"{exc.response.status_code if exc.response is not None else 'unknown'}."
                ),
            )
        except (requests.RequestException, json.JSONDecodeError) as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")
        raw = [
            {
                "title": item.get("title") or item.get("url") or "",
                "url": item.get("url") or "",
                "snippet": item.get("content") or "",
                "published_date": item.get("published_date"),
            }
            for item in data.get("results", [])
        ]
        return _results_response(
            query, self.name, raw, max_results, source_label=self.source_label,
        )


class SerperSearchProvider(WebSearchProvider):
    name = "serper"

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = _settings_key(self.name)
        self.timeout_seconds = settings.web_fetch_timeout_seconds

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        if not self.api_key:
            return WebSearchResponse(
                query=query, provider=self.name, error="Serper requires SERPER_API_KEY."
            )
        payload: dict[str, object] = {"q": query, "num": max_results}
        if time_filter in {"day", "week", "month", "year"}:
            payload["tbs"] = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}[
                time_filter
            ]
        try:
            response = self._http("POST",
                f"{SERPER_ORIGIN}/search",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout_seconds,
                credential=ServiceCredential.header(
                    self.name, self.api_key, origin=SERPER_ORIGIN, header="X-API-KEY"
                ),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, json.JSONDecodeError) as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")
        raw = [
            {
                "title": item.get("title") or item.get("link") or "",
                "url": item.get("link") or "",
                "snippet": item.get("snippet") or "",
                "published_date": item.get("date"),
            }
            for item in data.get("organic", [])
        ]
        return _results_response(
            query, self.name, raw, max_results, source_label=self.source_label,
        )


class WikipediaSearchProvider(WebSearchProvider):
    """Wikipedia's own search API: keyless, and published for programs to call.

    The last fallback in the chain. The general web providers above it scrape
    pages that block, rate-limit or degrade scripted requests on some networks,
    and when every one of them fails a question with an encyclopedic answer
    should still find one. Wikimedia asks API clients to identify themselves,
    which the configured user agent does.
    """

    name = "wikipedia"
    endpoint = "https://en.wikipedia.org/w/api.php"
    article_base = "https://en.wikipedia.org/wiki/"

    def __init__(self, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        try:
            response = self._http("GET",
                self.endpoint,
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": max(1, min(max_results, 20)),
                    "srprop": "snippet|timestamp",
                    "format": "json",
                    "formatversion": 2,
                },
                headers={"Accept": "application/json", "User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout:
            return WebSearchResponse(
                query=query, provider=self.name, error="Search provider timed out."
            )
        except (requests.RequestException, ValueError) as exc:
            return WebSearchResponse(query=query, provider=self.name, error=f"Search failed: {exc}")

        raw = [
            {
                "title": item["title"],
                "url": self.article_base + quote(item["title"].replace(" ", "_"), safe="_(),'"),
                # Snippets mark matched words with <span> tags.
                "snippet": unescape(re.sub(r"<[^>]+>", "", item.get("snippet") or "")),
                # The last edit: how current the article is, which is what a
                # freshness check needs to know.
                "published_date": item.get("timestamp"),
            }
            for item in (payload.get("query") or {}).get("search", [])
            if item.get("title")
        ]
        return _results_response(
            query, self.name, raw, max_results, source_label=self.source_label,
        )


class DisabledSearchProvider(WebSearchProvider):
    name = "disabled"

    def search(
        self,
        query: str,
        max_results: int,
        time_filter: str | None = None,
        vertical: str | None = None,
    ) -> WebSearchResponse:
        return WebSearchResponse(
            query=query,
            provider=self.name,
            error="Web search is disabled in this runtime.",
        )


class ProviderRegistry:
    def __init__(
        self, source_label: Label | None = None, *, grants: GrantStore | None = None
    ) -> None:
        self.settings = get_settings()
        self.source_label = label_of(source_label)
        self.grants = grants

    def provider(self, name: str) -> WebSearchProvider:
        provider = self._provider(name)
        provider.source_label = self.source_label
        provider.grants = self.grants
        return provider

    def _provider(self, name: str) -> WebSearchProvider:
        normalized = name.lower().strip()
        if normalized == "external_searxng":
            return SearXNGSearchProvider(provider_name="external_searxng")
        if normalized == "searxng":
            # Imported here rather than at module scope: searxng_embedded imports
            # WebSearchProvider and _results_response from this module.
            from app.services.search.searxng_embedded import EmbeddedSearXNGProvider

            return EmbeddedSearXNGProvider()
        if normalized == "tavily":
            return TavilySearchProvider()
        if normalized == "brave":
            return BraveSearchProvider()
        if normalized in {"duckduckgo", "ddg"}:
            return DuckDuckGoSearchProvider()
        if normalized in {"bing", "bing_html"}:
            return BingHTMLSearchProvider()
        if normalized == "serper":
            return SerperSearchProvider()
        if normalized == "wikipedia":
            return WikipediaSearchProvider()
        return DisabledSearchProvider()

    def primary_provider(self) -> WebSearchProvider:
        if not self.settings.web_search_enabled:
            return DisabledSearchProvider()
        return self.provider(self.settings.web_search_provider)

    def chain(self) -> list[WebSearchProvider]:
        if not self.settings.web_search_enabled:
            return [DisabledSearchProvider()]
        names = [self.settings.web_search_provider.lower().strip()]
        if names[0] == "disabled":
            return [DisabledSearchProvider()]
        configured = self.settings.web_search_fallback_providers
        fallback_names = [item.strip().lower() for item in configured.split(",") if item.strip()]
        if names[0] != "tavily":
            fallback_names = [name for name in fallback_names if name != "tavily"]
        for name in fallback_names:
            if name not in names and name != "disabled":
                names.append(name)
        if self.settings.web_search_wikipedia_fallback and "wikipedia" not in names:
            names.append("wikipedia")
        return [self.provider(name) for name in names]

    def list_providers(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for provider_id, (label, needs_key, _) in PROVIDER_INFO.items():
            rows.append(
                {
                    "id": provider_id,
                    "label": label,
                    "needs_key": needs_key,
                    "available": provider_id == "disabled" or provider_available(provider_id),
                    "active": provider_id == self.settings.web_search_provider.lower().strip(),
                }
            )
        return rows


def _results_response(
    query: str,
    provider: str,
    raw_results: list[dict[str, str | None]],
    max_results: int,
    *, source_label: Label | None = None,
) -> WebSearchResponse:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw_results:
        item_url = str(item.get("url") or "")
        if not item_url or item_url in seen:
            continue
        seen.add(item_url)
        results.append(
            SearchResult(
                source_label_json=to_json(label_of(source_label)),
                title=str(item.get("title") or item_url),
                url=item_url,
                snippet=item.get("snippet"),
                source=urlparse(item_url).netloc,
                published_date=item.get("published_date"),
                rank=len(results) + 1,
            )
        )
        if len(results) >= max_results:
            break
    return WebSearchResponse(
        source_label_json=to_json(label_of(source_label)),
        query=query,
        provider=provider,
        results=results,
        error=None if results else "Search returned no results.",
    )
