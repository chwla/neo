from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.source_citations import CitedAnswer
from app.services.web_fetch import FetchedPage, WebPageFetcher
from app.services.web_search import SearchResult, WebAnswerService, WebSearchService

router = APIRouter(prefix="/web", tags=["web"])


class WebSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_results: int | None = Field(default=None, ge=1, le=10)


class WebFetchRequest(BaseModel):
    url: str = Field(min_length=1)


class WebAnswerRequest(BaseModel):
    query: str = Field(min_length=1)


class WebSearchApiResponse(BaseModel):
    enabled: bool
    provider: str
    query: str
    results: list[SearchResult]
    error: str | None = None


@router.post("/search", response_model=WebSearchApiResponse)
def search_web(request: WebSearchRequest) -> WebSearchApiResponse:
    settings = get_settings()
    response = WebSearchService().search(request.query, request.max_results)
    return WebSearchApiResponse(
        enabled=settings.web_search_enabled,
        provider=response.provider,
        query=response.query,
        results=response.results,
        error=response.error,
    )


@router.post("/fetch", response_model=FetchedPage)
def fetch_web_page(request: WebFetchRequest) -> FetchedPage:
    return WebPageFetcher().fetch(request.url)


@router.post("/answer", response_model=CitedAnswer)
def answer_from_web(request: WebAnswerRequest) -> CitedAnswer:
    return WebAnswerService().answer(request.query)
