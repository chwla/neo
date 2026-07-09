from __future__ import annotations

import time

from app.services.llm_registry.providers import build_client
from app.services.llm_registry.service import LLMRegistryService
from app.services.llm_registry.usage import safe_error


def check_health(*, route_name=None, provider_id=None, model_id=None) -> dict:
    service = LLMRegistryService()
    if route_name:
        route = service.resolve(route_name)
        provider_id, model_id = route["provider_id"], route["model_id"]
    if not provider_id or not model_id:
        raise ValueError("Provide route_name or both provider_id and model_id.")
    provider, model = service.get_provider(provider_id), service.get_model(model_id)
    if not provider or not model or model["provider_id"] != provider["id"]:
        raise LookupError("Configured provider/model was not found.")
    started = time.perf_counter()
    error = None
    try:
        client = build_client(provider, model)
        provider_available = client.is_available()
        model_available = provider_available and client.model_is_installed()
        available = provider_available and model_available
        if not provider_available:
            error = "Provider is unavailable."
        elif not model_available:
            error = "Configured model is unavailable from the provider."
    except Exception as exc:
        available, provider_available, model_available = False, False, False
        error = safe_error(exc, provider)
    return {
        "provider": provider["provider_type"],
        "provider_id": provider["id"],
        "model": model["model_name"],
        "model_id": model["id"],
        "available": available,
        "provider_available": provider_available,
        "model_available": model_available,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }
