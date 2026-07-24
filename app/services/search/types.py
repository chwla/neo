from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from app.services.source_citations import SourceCitation


class SearchIntentKind(StrEnum):
    NONE = "none"
    GENERAL_WEB = "general_web"
    RELEASE_DATE = "release_date"
    WEATHER = "weather"
    CURRENCY = "currency"
    LOCAL_DATETIME = "local_datetime"
    CONNECTOR_TOOL = "connector_tool"


class ResolvedSearchIntent(BaseModel):
    """Structured, carryable result of resolving a user turn's live-data intent."""

    kind: SearchIntentKind
    original_query: str
    resolved_query: str
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)
    entity: str | None = None
    location: str | None = None
    region: str | None = None
    date: str | None = None
    amount: Decimal | None = None
    from_currency: str | None = None
    to_currency: str | None = None
    timezone: str | None = None
    locale: str | None = None

    @property
    def needs_external_data(self) -> bool:
        return self.kind in {
            SearchIntentKind.GENERAL_WEB,
            SearchIntentKind.RELEASE_DATE,
            SearchIntentKind.WEATHER,
            SearchIntentKind.CURRENCY,
            SearchIntentKind.CONNECTOR_TOOL,
        }


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str | None = None
    source: str
    published_date: str | None = None
    rank: int
    relevance_score: float = 0.0
    relevance_reasons: list[str] = Field(default_factory=list)


class WebSearchResponse(BaseModel):
    query: str
    provider: str
    results: list[SearchResult] = Field(default_factory=list)
    error: str | None = None
    provider_query: str | None = None
    attempted_providers: dict[str, str] = Field(default_factory=dict)
    provider_attempts: list[dict[str, object]] = Field(default_factory=list)


class FetchedPage(BaseModel):
    url: str
    title: str | None = None
    domain: str
    text: str = ""
    fetched: bool = False
    content_type: str | None = None
    error: str | None = None


class EvidenceChunk(BaseModel):
    source_index: int = 0
    source_title: str
    source_url: str
    source: str
    text: str
    relevance_score: float


class WebContext(BaseModel):
    query: str
    needed: bool
    search: WebSearchResponse | None = None
    selected_results: list[SearchResult] = Field(default_factory=list)
    pages: list[FetchedPage] = Field(default_factory=list)
    evidence_chunks: list[EvidenceChunk] = Field(default_factory=list)
    citations: list[SourceCitation] = Field(default_factory=list)
    context_text: str = ""
    answer_mode: str = "unknown"
    warning: str | None = None


class WebSearchDecision(BaseModel):
    needed: bool
    reason: str


class QueryRelevanceProfile(BaseModel):
    query: str
    provider_query: str
    terms: list[str]
    aliases: list[str]
    requires_freshness: bool = False


class SearchOptions(BaseModel):
    max_results: int | None = None
    max_pages: int | None = None
    time_filter: str | None = None
    min_content_length: int = 80


class StructuredSource(BaseModel):
    index: int
    title: str
    url: str
    source: str
    evidence_count: int = 0


class ComprehensiveSearchResult(BaseModel):
    query: str
    provider_used: str
    rewritten_query: str
    raw_results: list[SearchResult] = Field(default_factory=list)
    ranked_results: list[SearchResult] = Field(default_factory=list)
    fetched_pages: list[FetchedPage] = Field(default_factory=list)
    evidence_chunks: list[EvidenceChunk] = Field(default_factory=list)
    structured_sources: list[StructuredSource] = Field(default_factory=list)
    citations: list[SourceCitation] = Field(default_factory=list)
    model_context: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    debug: dict[str, object] = Field(default_factory=dict)
