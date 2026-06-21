from __future__ import annotations

from app.services.search.content import WebPageFetcher
from app.services.search.core import comprehensive_web_search
from app.services.search.types import SearchOptions


def web_search(query: str, time_filter: str | None = None) -> dict[str, object]:
    result = comprehensive_web_search(query, SearchOptions(time_filter=time_filter))
    return result.model_dump()


def web_fetch(url: str) -> dict[str, object]:
    return WebPageFetcher().fetch(url).model_dump()


class WebSearchTool:
    def execute(self, query: str, time_filter: str | None = None) -> dict[str, object]:
        return web_search(query, time_filter)


class WebFetchTool:
    def execute(self, url: str) -> dict[str, object]:
        return web_fetch(url)
