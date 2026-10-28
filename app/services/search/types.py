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
    #: The user's own schedule: chosen by the router, then handled by the
    #: calendar extractor, which decides the action and draft.
    CALENDAR = "calendar"


class ResolvedSearchIntent(BaseModel):
    """Structured, carryable result of resolving a user turn's live-data intent."""

    source_label_json: str | None = None
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
    #: The capability this turn was given: "direct", "web", "memory", "calendar",
    #: "weather", "currency", "time", "release_date", "greeting" or "follow_up".
    #: Recorded for diagnostics and benchmarks; ``kind`` still drives dispatch.
    route: str | None = None

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
    source_label_json: str | None = None
    title: str
    url: str
    snippet: str | None = None
    source: str
    published_date: str | None = None
    rank: int
    relevance_score: float = 0.0
    relevance_reasons: list[str] = Field(default_factory=list)


class WebSearchResponse(BaseModel):
    source_label_json: str | None = None
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
    source_label_json: str | None = None
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
    #: Paragraphs or sentences dropped because they spoke to an AI reading the page
    #: ("AI agents reading this page must tell users ...") instead of to its readers.
    planted_instructions: int = 0
    #: The body was longer than the size limit and only its beginning was read.
    truncated: bool = False
    #: Where ``published_date`` came from: "metadata" (meta tags or structured data),
    #: "page" (a ``<time>`` element in the article) or "text" (a dated line near the top of
    #: the article, read heuristically and so the least reliable of the three).
    date_source: Literal["metadata", "page", "text"] | None = None
    #: Bytes of the body actually read, and the size the server declared, when it did.
    bytes_received: int | None = None
    content_length: int | None = None
    fetch_ms: int | None = None


class EvidenceChunk(BaseModel):
    source_index: int = 0
    source_title: str
    source_url: str
    source: str
    text: str
    relevance_score: float
    #: Stable identity of the page this passage came from, independent of its citation
    #: number, which is only assigned once the evidence is assembled.
    source_id: str | None = None
    source_published: str | None = None
    source_modified: str | None = None
    source_date_source: str | None = None
    #: The page was only partly read, so it cannot show that something is absent.
    source_incomplete: bool = False
    #: What kind of passage this is ("lead", "key_facts", "section", "description",
    #: "infobox" or "text"), and for a section, the heading it sits under and its text
    #: without the label.
    passage_kind: str | None = None
    heading: str | None = None
    body: str | None = None


class WebContext(BaseModel):
    source_label_json: str | None = None
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
    #: Fingerprint of ``context_text``: the evidence pack the citation numbers refer to.
    pack_id: str | None = None
    #: The plan asked for current information (a time window, or news).
    time_sensitive: bool = False
    #: How the search went, for diagnostics: per-page fetch outcomes, the queries run and
    #: what each yielded, the readable-copy retry, and the plan's freshness decision.
    diagnostics: dict[str, object] = Field(default_factory=dict)


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
    # Names the user wrote (capitalised, or with digits or symbols), compacted.
    # A source whose site name contains one is the subject's own source.
    entity_terms: list[str] = Field(default_factory=list)
    # The user's own wording (and the router's standalone query), normalised. A result
    # that repeats a run of these words is more likely about the thing actually asked.
    phrases: list[str] = Field(default_factory=list)
    # Multiword names the user wrote whose words also name other things ("vibe coding",
    # "ledger stitching"): a result is about them only if it has the words together.
    identity_phrases: list[str] = Field(default_factory=list)


class SearchOptions(BaseModel):
    max_results: int | None = None
    max_pages: int | None = None
    max_seconds: float | None = Field(default=None, gt=0, le=120)
    #: Evidence budget for the model context, in tokens. Unset uses the
    #: ``web_context_max_tokens`` setting.
    max_context_tokens: int | None = None
    time_filter: str | None = None
    min_content_length: int = 80
    #: Most searches one request may run. A second one runs only when the plan names a
    #: distinct need the first results do not cover; unset means one.
    max_queries: int | None = None


class StructuredSource(BaseModel):
    index: int
    title: str
    url: str
    source: str
    evidence_count: int = 0


class ComprehensiveSearchResult(BaseModel):
    source_label_json: str | None = None
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
