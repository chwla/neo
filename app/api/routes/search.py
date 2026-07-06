from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.services.search import (
    ComprehensiveSearchResult,
    ProviderRegistry,
    SearchOptions,
    WebPageFetcher,
    comprehensive_web_search,
)
from app.services.search.providers import PROVIDER_INFO, normalize_searxng_instance

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


class SearchConfigUpdateRequest(BaseModel):
    provider: str | None = None
    searxng_instance: str | None = None
    tavily_key: str | None = None


class SearchTestRequest(BaseModel):
    query: str = Field(default="latest OpenAI news", min_length=1)
    count: int = Field(default=5, ge=1, le=10)
    time_filter: str | None = None


@router.get("/config")
def search_config() -> dict[str, object]:
    settings = get_settings()
    return {
        "enabled": settings.web_search_enabled,
        "provider": settings.web_search_provider,
        "searxng_instance": settings.searxng_instance,
        "tavily_configured": bool(settings.tavily_api_key or settings.web_search_api_key),
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


@router.post("/config")
def update_search_config(request: SearchConfigUpdateRequest) -> dict[str, object]:
    settings = get_settings()
    provider = (request.provider or settings.web_search_provider).strip().lower()
    if provider not in {"disabled", "external_searxng", "searxng", "tavily"}:
        raise HTTPException(
            status_code=422,
            detail="Provider must be disabled, external_searxng, searxng, or tavily.",
        )

    searxng_instance = settings.searxng_instance
    if request.searxng_instance is not None:
        try:
            searxng_instance = normalize_searxng_instance(request.searxng_instance)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    tavily_key = settings.tavily_api_key or ""
    if request.tavily_key is not None:
        tavily_key = request.tavily_key.strip()
    if provider == "tavily" and not (tavily_key or settings.web_search_api_key):
        raise HTTPException(status_code=422, detail="Tavily requires TAVILY_API_KEY.")

    settings.web_search_provider = provider
    settings.searxng_instance = searxng_instance
    settings.tavily_api_key = tavily_key or None
    return search_config()


@router.post("/test")
def test_search_provider(request: SearchTestRequest) -> dict[str, object]:
    settings = get_settings()
    provider = ProviderRegistry().provider(settings.web_search_provider)
    started = perf_counter()
    response = provider.search(request.query, request.count, request.time_filter)
    latency_ms = round((perf_counter() - started) * 1000)
    available = bool(response.results and not response.error)
    return {
        "success": available,
        "available": available,
        "provider": response.provider,
        "provider_used": response.provider,
        "result_count": len(response.results),
        "latency_ms": latency_ms,
        "error": response.error,
        "message": response.error or "Configured web search provider is available.",
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
