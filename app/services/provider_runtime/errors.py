from __future__ import annotations

import requests

from app.services.llm_registry.providers import ProviderConfigurationError
from app.services.provider_runtime.redaction import safe_text


class ContextTooLargeError(RuntimeError):
    """The request cannot fit in the model's context window.

    Carried as its own type so the caller can answer with the limit instead of
    reporting an internal failure: the user asked for something too big, which is a
    normal outcome rather than a bug in Neo.
    """


def classify(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, ProviderConfigurationError) or any(
        key in message for key in ("api key", "auth", "unauthorized", "forbidden", "configured")
    ):
        return "auth_or_config"
    if "rate" in message or "429" in message:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, requests.Timeout)) or "timeout" in message:
        return "timeout"
    if "context" in message and ("large" in message or "length" in message):
        return "context_too_large"
    if "unsupported" in message or "capability" in message:
        return "unsupported_capability"
    if isinstance(exc, (ConnectionError, requests.RequestException)):
        return "transient_network"
    return "provider_error"


def safe_error(exc: Exception) -> tuple[str, str, dict]:
    message, summary = safe_text(exc, 800)
    return classify(exc), message, summary
