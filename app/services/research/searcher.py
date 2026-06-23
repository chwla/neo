"""Multi-query search orchestrator reusing the existing search pipeline."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from app.core.config import get_settings
from app.services.search.content import WebPageFetcher
from app.services.search.core import comprehensive_web_search
from app.services.search.types import (
    ComprehensiveSearchResult,
    FetchedPage,
    SearchOptions,
    SearchResult,
)
from app.services.research.types import ResearchSource

logger = logging.getLogger(__name__)

_NON_RETRYABLE_ERRORS = frozenset({
    "Invalid or unsupported URL.",
    "Blocked unsafe or unsupported URL.",
    "Blocked unsafe redirect.",
})

_NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 405, 410, 451})


class ResearchSearcher:
    """Runs multiple search queries and aggregates + deduplicates results."""

    def __init__(self, max_sources: int = 10) -> None:
        self.max_sources = max_sources
        settings = get_settings()
        self.fetcher = WebPageFetcher(
            timeout_seconds=settings.research_fetch_timeout_seconds,
        )
        self.max_workers = settings.research_max_fetch_workers
        self.max_retries = settings.research_fetch_retries
        self._seen_urls: set[str] = set()
        self._seen_domains_titles: set[str] = set()

    def search_query(self, query: str, max_results: int = 8) -> ComprehensiveSearchResult:
        opts = SearchOptions(max_results=max_results, max_pages=0)
        return comprehensive_web_search(query, opts)

    def search_multiple(
        self,
        queries: list[str],
        on_query_done: callable | None = None,
        cancelled: callable | None = None,
    ) -> list[SearchResult]:
        all_results: list[SearchResult] = []

        for i, query in enumerate(queries):
            if cancelled and cancelled():
                break
            try:
                result = self.search_query(query)
                new_results = self._deduplicate_results(result.ranked_results or result.raw_results)
                all_results.extend(new_results)
            except Exception:
                logger.exception("Search failed for query: %s", query)

            if on_query_done:
                on_query_done(i + 1, len(queries), query)

        return all_results

    def fetch_sources(
        self,
        results: list[SearchResult],
        max_pages: int | None = None,
        cancelled: callable | None = None,
    ) -> list[ResearchSource]:
        limit = self.max_sources if max_pages is None else max_pages
        if limit <= 0:
            return []
        seen: set[str] = set()
        to_fetch: list[SearchResult] = []
        for r in results:
            canonical = _canonical_url(r.url)
            if canonical not in seen:
                seen.add(canonical)
                to_fetch.append(r)
        to_fetch = to_fetch[:limit * 2]

        sources: list[ResearchSource] = []
        fetched_count = 0

        with ThreadPoolExecutor(max_workers=min(limit, self.max_workers)) as executor:
            future_map = {
                executor.submit(self._fetch_with_retry, r.url): r
                for r in to_fetch
            }
            for future in as_completed(future_map):
                if cancelled and cancelled():
                    break
                result = future_map[future]
                try:
                    page, attempts, error_detail = future.result()
                except Exception as exc:
                    sources.append(ResearchSource(
                        url=result.url,
                        title=result.title,
                        domain=result.source,
                        fetch_status="failed",
                        fetch_error=str(exc),
                        error=str(exc),
                    ))
                    continue

                source = ResearchSource(
                    url=page.url,
                    title=page.title or result.title,
                    domain=page.domain,
                    fetched=page.fetched,
                    fetch_status="success" if page.fetched and page.text else "failed",
                    fetch_error=page.error or error_detail,
                    content_type=page.content_type,
                    text=page.text if page.fetched else "",
                    extracted_text_length=len(page.text) if page.text else 0,
                    relevance_score=result.relevance_score,
                )

                if page.fetched and page.text:
                    source.quality_score = _score_source_quality(page, result)
                    fetched_count += 1
                elif not page.fetched:
                    source.fetch_status = "failed"
                    source.fetch_error = page.error or "No content extracted"

                sources.append(source)
                if fetched_count >= limit:
                    break

        for i, src in enumerate(sources):
            src.id = i + 1

        return sources

    def _fetch_with_retry(self, url: str) -> tuple[FetchedPage, int, str | None]:
        last_error: str | None = None
        for attempt in range(1 + self.max_retries):
            page = self.fetcher.fetch(url)

            if page.fetched and page.text:
                return page, attempt + 1, None

            if page.error:
                last_error = page.error
                if page.error in _NON_RETRYABLE_ERRORS:
                    return page, attempt + 1, page.error
                if page.content_type and "text" not in page.content_type and "json" not in page.content_type:
                    return page, attempt + 1, f"Unsupported content type: {page.content_type}"

            if attempt < self.max_retries:
                time.sleep(1.0)

        return page, 1 + self.max_retries, last_error

    def _deduplicate_results(self, results: list[SearchResult]) -> list[SearchResult]:
        unique: list[SearchResult] = []
        for r in results:
            canonical = _canonical_url(r.url)
            if canonical in self._seen_urls:
                continue
            domain_title = f"{r.source}:{r.title.lower()[:60]}" if r.title else ""
            if domain_title and domain_title in self._seen_domains_titles:
                continue
            self._seen_urls.add(canonical)
            if domain_title:
                self._seen_domains_titles.add(domain_title)
            unique.append(r)
        return unique


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc}{path}".lower()


_OFFICIAL_DOMAINS = frozenset({
    "github.com", "docs.python.org", "developer.mozilla.org",
    "arxiv.org", "stackoverflow.com", "en.wikipedia.org",
    "docs.microsoft.com", "learn.microsoft.com",
    "docs.google.com", "cloud.google.com",
    "aws.amazon.com", "huggingface.co",
    "nextjs.org", "fastapi.tiangolo.com", "postgresql.org",
    "ollama.com", "tavily.com", "docs.searxng.org",
})

_SPAM_PATTERNS = frozenset({
    "pinterest.com", "quora.com", "facebook.com",
    "instagram.com", "tiktok.com", "youtube.com",
})


def _score_source_quality(page: FetchedPage, result: SearchResult) -> float:
    score = 5.0
    domain = page.domain.lower()

    if any(d in domain for d in _OFFICIAL_DOMAINS):
        score += 3.0
    if any(d in domain for d in _SPAM_PATTERNS):
        score -= 3.0

    if domain.endswith(".gov") or domain.endswith(".edu") or domain.endswith(".org"):
        score += 1.5

    text_len = len(page.text) if page.text else 0
    if text_len > 2000:
        score += 1.5
    elif text_len > 500:
        score += 0.5
    elif text_len < 100:
        score -= 2.0

    if result.relevance_score > 0.7:
        score += 1.0

    return max(0.0, min(10.0, score))
