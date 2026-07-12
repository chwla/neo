from __future__ import annotations

import time

from app.services.llm_registry.providers import build_client
from app.services.provider_runtime import store
from app.services.provider_runtime.errors import safe_error
from app.services.provider_runtime.router import select


def check(
    route_name: str | None = None, provider_id: str | None = None, model_id: str | None = None
) -> dict:
    route = select("chat", route_name) if route_name else None
    if not route and provider_id and model_id:
        from app.services.llm_registry.service import LLMRegistryService

        service = LLMRegistryService()
        provider, model = service.get_provider(provider_id), service.get_model(model_id)
        if not provider or not model:
            raise LookupError("Configured provider/model was not found.")
        route = {
            "route_name": None,
            "provider": provider["provider_type"],
            "model": model["model_name"],
            "provider_record": provider,
            "model_record": model,
        }
    if not route:
        raise ValueError("Provide route_name or both provider_id and model_id.")
    started = time.perf_counter()
    category = message = None
    try:
        client = build_client(route["provider_record"], route["model_record"])
        provider_ok = client.is_available()
        model_ok = provider_ok and client.model_is_installed()
        state = "healthy" if provider_ok and model_ok else "unavailable"
        if not provider_ok:
            message = "Provider is unavailable."
        elif not model_ok:
            state, message = "degraded", "Configured model is unavailable."
    except Exception as exc:
        category, message, _ = safe_error(exc)
        state, provider_ok, model_ok = "misconfigured", False, False
    result = {
        "route_name": route.get("route_name"),
        "provider_name": route["provider"],
        "model_name": route["model"],
        "status": state,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error_category": category,
        "error_message": message,
        "metadata": {"provider_available": provider_ok, "model_available": model_ok},
    }
    return store.add_health(result)
