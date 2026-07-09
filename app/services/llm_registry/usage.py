from __future__ import annotations

import os
import uuid

from app.services.llm_registry import store


def safe_error(error: Exception, provider: dict | None = None) -> str:
    text = str(error)
    if provider:
        ref = provider.get("api_key_ref")
        secret = os.getenv(ref) if ref else None
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:2000]


def record_call(
    *,
    route_name: str,
    provider_id: str | None,
    model_id: str | None,
    status: str,
    latency_ms: int | None = None,
    result=None,
    error: str | None = None,
    fallback_used: bool = False,
) -> dict:
    return store.insert_call(
        {
            "id": str(uuid.uuid4()),
            "route_name": route_name,
            "provider_id": provider_id,
            "model_id": model_id,
            "status": status,
            "prompt_tokens": getattr(result, "prompt_tokens", None),
            "completion_tokens": getattr(result, "completion_tokens", None),
            "total_tokens": getattr(result, "total_tokens", None),
            "latency_ms": latency_ms
            if latency_ms is not None
            else getattr(result, "duration_ms", None),
            "error": error,
            "fallback_used": fallback_used,
            "created_at": store.now_iso(),
        }
    )
