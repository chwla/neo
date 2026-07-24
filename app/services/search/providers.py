from __future__ import annotations

import json
from abc import ABC, abstractmethod
from base64 import urlsafe_b64decode
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from app.core.config import get_settings
from app.services.search.types import SearchResult, WebSearchResponse

PROVIDER_INFO = {
    "external_searxng": ("External SearXNG", False, True),
    "searxng": ("SearXNG (legacy alias)", False, True),
    "tavily": ("Tavily", True, False),
    "brave": ("Brave Search", True, False),
    "duckduckgo": ("DuckDuckGo", False, False),
    "bing_html": ("Bing HTML", False, False),
    "serper": ("Serper", True, False),
    "disabled": ("Disabled", False, False),
}


class WebSearchProvider(ABC):
    name: str

    @abstractmethod
    def search(
        self, query: str, max_results: int, time_filter: str | None = None
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
    if provider in {"external_searxng", "searxng"}:
        try:
            normalize_searxng_instance(get_settings().searxng_instance)
        except ValueError:
            return False
        return True
    return not info[1] or bool(_settings_key(provider))


class SearXNGSearchProvider(WebSearchProvider):
    name = "searxng"

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
        self, query: str, max_results: int, time_filter: str | None = None
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
        try:
            response = requests.get(
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
        return _results_response(query, self.name, raw, max_results)


class DuckDuckGoSearchProvider(WebSearchProvider):
    name = "duckduckgo"

    def __init__(self, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
    ) -> WebSearchResponse:
        try:
            response = requests.get(
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
        return _results_response(query, self.name, parser.results, max_results)


class BingHTMLSearchProvider(WebSearchProvider):
    name = "bing_html"

    def __init__(self, timeout_seconds: float | None = None, user_agent: str | None = None) -> None:
        settings = get_settings()
        self.timeout_seconds = timeout_seconds or settings.web_fetch_timeout_seconds
        self.user_agent = user_agent or settings.web_search_user_agent

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
    ) -> WebSearchResponse:
        try:
            response = requests.get(
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
        return _results_response(query, self.name, parser.results, max_results)


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
        self, query: str, max_results: int, time_filter: str | None = None
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
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": self.user_agent,
                    "X-Subscription-Token": self.api_key,
                },
                timeout=self.timeout_seconds,
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
        return _results_response(query, self.name, raw, max_results)


class TavilySearchProvider(WebSearchProvider):
    name = "tavily"

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = _settings_key(self.name)
        self.timeout_seconds = settings.web_fetch_timeout_seconds

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
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
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout_seconds,
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
        return _results_response(query, self.name, raw, max_results)


class SerperSearchProvider(WebSearchProvider):
    name = "serper"

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = _settings_key(self.name)
        self.timeout_seconds = settings.web_fetch_timeout_seconds

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
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
            response = requests.post(
                "https://google.serper.dev/search",
                json=payload,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                timeout=self.timeout_seconds,
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
        return _results_response(query, self.name, raw, max_results)


class DisabledSearchProvider(WebSearchProvider):
    name = "disabled"

    def search(
        self, query: str, max_results: int, time_filter: str | None = None
    ) -> WebSearchResponse:
        return WebSearchResponse(
            query=query,
            provider=self.name,
            error="Web search is disabled in this runtime.",
        )


class ProviderRegistry:
    def __init__(self) -> None:
        self.settings = get_settings()

    def provider(self, name: str) -> WebSearchProvider:
        normalized = name.lower().strip()
        if normalized == "external_searxng":
            return SearXNGSearchProvider(provider_name="external_searxng")
        if normalized == "searxng":
            return SearXNGSearchProvider()
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
        query=query,
        provider=provider,
        results=results,
        error=None if results else "Search returned no results.",
    )
