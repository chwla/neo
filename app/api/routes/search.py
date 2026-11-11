from __future__ import annotations

from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.routes.accounts import session_for
from app.core.config import get_settings
from app.services import search_prefs
from app.services.disclosure import Labeled, content_hash, public
from app.services.search import (
    ComprehensiveSearchResult,
    ProviderRegistry,
    SearchOptions,
    comprehensive_web_search,
)
from app.services.search.providers import (
    PROVIDER_INFO,
    normalize_searxng_instance,
    provider_available,
)

router = APIRouter(prefix="/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    max_results: int | None = Field(default=None, ge=1, le=10)
    max_pages: int | None = Field(default=None, ge=0, le=5)
    time_filter: str | None = None


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


def _query_label(payload: BaseModel) -> object:
    """The query as what it is: text the signed-in user typed this request.

    Without a label the provider's outbound request is of unknown provenance, and
    the disclosure gate refuses it -- which is what made the Settings "Test" button
    report every provider as broken.
    """

    return public("user_input:" + content_hash(payload.model_dump(mode="json")))


@router.post("/config")
def update_search_config(
    request: SearchConfigUpdateRequest, http_request: Request
) -> dict[str, object]:
    profile = session_for(http_request)
    if profile is None or profile.get("is_guest"):
        raise HTTPException(
            status_code=403,
            detail="Guest sessions cannot change web search settings. Create a profile first.",
        )
    settings = get_settings()
    provider = (request.provider or settings.web_search_provider).strip().lower()
    if provider not in PROVIDER_INFO:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown search provider: {provider}.",
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

    # Stored for this profile only. Writing the process-wide settings object made
    # one account's choice -- and its key -- every account's, a guest's included.
    search_prefs.save(
        provider=provider, searxng_instance=searxng_instance, tavily_api_key=tavily_key or None
    )
    return search_config()


def _test_search_provider(request: SearchTestRequest) -> dict[str, object]:
    settings = get_settings()
    name = settings.web_search_provider
    if name == "disabled":
        return {
            "success": False, "available": False, "provider": name, "provider_used": name,
            "result_count": 0, "latency_ms": 0, "error": "Web search is turned off.",
            "message": "Web search is turned off. Choose a provider first.",
        }
    if not provider_available(name):
        reason = (
            "The embedded SearXNG is not set up on this computer (no SearXNG source tree). "
            "Choose another provider, or run scripts/setup_searxng.py."
            if name == "searxng"
            else f"The {name} provider is not configured (a key or an instance URL is missing)."
        )
        return {
            "success": False, "available": False, "provider": name, "provider_used": name,
            "result_count": 0, "latency_ms": 0, "error": reason, "message": reason,
        }
    started = perf_counter()
    try:
        provider = ProviderRegistry(_query_label(request)).provider(name)
        response = provider.search(request.query, request.count, request.time_filter)
    except Exception as exc:  # noqa: BLE001 - a provider fault is reported, not raised
        reason = f"The {name} provider failed: {exc}"
        return {
            "success": False, "available": False, "provider": name, "provider_used": name,
            "result_count": 0, "latency_ms": round((perf_counter() - started) * 1000),
            "error": reason, "message": reason,
        }
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


@router.get("/test")
def test_search_provider_get(
    query: str = "latest OpenAI news",
    count: int = 5,
    time_filter: str | None = None,
) -> dict[str, object]:
    return _test_search_provider(
        SearchTestRequest(query=query, count=count, time_filter=time_filter)
    )


@router.post("/test")
def test_search_provider(request: SearchTestRequest) -> dict[str, object]:
    return _test_search_provider(request)


@router.get("/providers")
def search_providers() -> list[dict[str, object]]:
    return ProviderRegistry().list_providers()


@router.post("", response_model=ComprehensiveSearchResult)
def search(request: SearchRequest) -> ComprehensiveSearchResult:
    return comprehensive_web_search(
        Labeled(request.query, _query_label(request)),
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
    provider = ProviderRegistry(_query_label(request)).provider(request.provider)
    try:
        response = provider.search(request.query, request.count, request.time_filter)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, never a 500
        return {"results": [], "provider": request.provider, "error": str(exc)}
    return response.model_dump()
