from __future__ import annotations

from app.services.llm_registry.service import LLMRegistryService

_ROUTES = {
    "coding": "coding_agent",
    "research": "research",
    "summary": "summarization",
    "memory": "summarization",
    "search": "research",
    "tool_reasoning": "agent",
    "embedding_if_available": "embedding",
}


def select(request_type: str, route_name: str | None = None) -> dict:
    name = route_name or _ROUTES.get(request_type, "chat")
    service = LLMRegistryService()
    route = service.resolve(name)
    provider, model = (
        service.get_provider(route["provider_id"]),
        service.get_model(route["model_id"]),
    )
    if not provider or not model:
        raise LookupError("Configured provider/model was not found.")
    fallback = [name]
    if route.get("fallback_provider_id") and route.get("fallback_model_id"):
        fallback.append("configured_fallback")
    if name != "chat":
        fallback.append("chat")
    return {
        "route_name": name,
        "provider": provider["provider_type"],
        "provider_id": provider["id"],
        "model": model["model_name"],
        "model_id": model["id"],
        "provider_record": provider,
        "model_record": model,
        "route_record": route,
        "fallback_chain": fallback,
        "reason": "selected configured route",
    }
