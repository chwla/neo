from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.search import (
    ComprehensiveSearchResult,
    ProviderRegistry,
    SearchOptions,
    SearchResult,
    WebPageFetcher,
    comprehensive_web_search,
)
from app.services.search.providers import PROVIDER_INFO

router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_results: int | None = Field(default=None, ge=1, le=10)
    max_pages: int | None = Field(default=None, ge=0, le=5)
    time_filter: str | None = None


class FetchRequest(BaseModel):
    url: str = Field(min_length=1)


class ProviderQueryRequest(BaseModel):
    query: str = Field(min_length=1)
    provider: str
    count: int = Field(default=10, ge=1, le=20)
    time_filter: str | None = None


@router.get("/config")
def search_config() -> dict[str, object]:
    settings = get_settings()
    return {
        "enabled": settings.web_search_enabled,
        "provider": settings.web_search_provider,
        "fallback_providers": [
            item.strip()
            for item in settings.web_search_fallback_providers.split(",")
            if item.strip()
        ],
        "max_results": settings.web_search_max_results,
        "fetch_max_pages": settings.web_fetch_max_pages,
        "fetch_timeout_seconds": settings.web_fetch_timeout_seconds,
        "fetch_max_bytes": settings.web_fetch_max_bytes,
        "user_agent": settings.web_search_user_agent,
        "cache_enabled": settings.web_cache_enabled,
        "has_keys": {
            "tavily": bool(settings.tavily_api_key or settings.web_search_api_key),
            "brave": bool(settings.brave_api_key or settings.web_search_api_key),
            "serper": bool(settings.serper_api_key or settings.web_search_api_key),
        },
    }


@router.get("/providers")
def search_providers() -> list[dict[str, object]]:
    return ProviderRegistry().list_providers()


@router.post("", response_model=ComprehensiveSearchResult)
def search(request: SearchRequest) -> ComprehensiveSearchResult:
    return comprehensive_web_search(
        request.query,
        SearchOptions(
            max_results=request.max_results,
            max_pages=request.max_pages,
            time_filter=request.time_filter,
        ),
    )


@router.post("/query")
def search_with_provider(request: ProviderQueryRequest) -> dict[str, object]:
    if request.provider not in PROVIDER_INFO or request.provider == "disabled":
        return {"results": [], "provider": request.provider, "error": "Unknown provider"}
    provider = ProviderRegistry().provider(request.provider)
    response = provider.search(request.query, request.count, request.time_filter)
    return response.model_dump()


@router.post("/fetch")
def fetch(request: FetchRequest):
    return WebPageFetcher().fetch(request.url)
