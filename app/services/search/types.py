from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

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
    decision_source: Literal["structured", "model", "fallback"] = "fallback"

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


class PageBlock(BaseModel):
    """One structural unit of a page, in document order."""

    kind: Literal["heading", "paragraph", "list_item", "table_row"] = "paragraph"
    text: str
    level: int = 0
    heading_path: list[str] = Field(default_factory=list)
    link_chars: int = 0


class PageLink(BaseModel):
    """An outbound link as it appeared in a page's readable content."""

    href: str
    text: str = ""
    #: Index into ``FetchedPage.blocks`` of the block the link sits in, when that block is kept.
    block_index: int | None = None


class FetchedPage(BaseModel):
    url: str
    title: str | None = None
    domain: str
    text: str = ""
    fetched: bool = False
    content_type: str | None = None
    error: str | None = None
    http_status: int | None = None
    # Structure and metadata for callers that need more than flattened text. All optional,
    # so pages built by older callers and tests keep working unchanged.
    blocks: list[PageBlock] = Field(default_factory=list)
    canonical_url: str | None = None
    site_name: str | None = None
    organization_names: list[str] = Field(default_factory=list)
    schema_types: list[str] = Field(default_factory=list)
    scholarly: bool = False
    published_date: str | None = None
    modified_date: str | None = None
    author: str | None = None
    lang: str | None = None
    #: Links in the readable content, reference lists included, in document order.
    links: list[PageLink] = Field(default_factory=list)
    #: The page's ``citation_*`` metadata (title, doi, pdf_url, publisher, ...), keys unprefixed.
    citation_meta: dict[str, str] = Field(default_factory=dict)
    document_kind: Literal["html", "pdf", "text"] = "html"
    #: The body was longer than the size limit and only its beginning was read.
    truncated: bool = False


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
    # Terms the planner identified as the actual topic. When present these decide
    # how many term matches a result needs; see rank_results.
    subject_terms: list[str] = Field(default_factory=list)


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
    answer_mode: str = ""
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    debug: dict[str, object] = Field(default_factory=dict)
