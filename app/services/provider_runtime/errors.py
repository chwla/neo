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


class ProviderFailure(RuntimeError):
    """A provider request that failed, carrying only text that is safe to show.

    ``str(exc)`` is the sentence the user reads, so it never carries the provider
    exception, a host, a port, a URL, or a stack detail. The original text stays on
    ``detail`` for the logs, which is where an operator debugging the failure looks.
    """

    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        detail: str = "",
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.category = category
        self.detail = detail
        self.provider = provider


#: Fixed labels keyed by provider *type*, never by the operator's own provider name or
#: its base URL: the label reaches the user, so it may only contain text Neo chose.
_PROVIDER_LABELS = {
    "ollama": "Ollama",
    "openai_compatible": "The model provider",
    "mock": "The mock provider",
    "disabled": "The model provider",
}
_DEFAULT_LABEL = "The model provider"
#: How to word "make it reachable again" for a provider the user runs themselves.
_RESTART_HINTS = {"ollama": "Check that Ollama is running, then try again."}
_DEFAULT_RESTART_HINT = "Check that it is reachable, then try again."

#: A connection that never reached the provider. ``requests`` wraps the urllib3 cause in
#: its own type, so match on both the type and the text the cause leaves behind.
_UNAVAILABLE_MARKERS = (
    "connection refused",
    "failed to establish a new connection",
    "max retries exceeded",
    "connection aborted",
    "connection reset",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no route to host",
    "network is unreachable",
)


def provider_label(provider_type: str | None) -> str:
    return _PROVIDER_LABELS.get(str(provider_type or "").strip().lower(), _DEFAULT_LABEL)


def classify(exc: Exception) -> str:
    message = str(exc).lower()
    if isinstance(exc, ProviderConfigurationError) or any(
        key in message for key in ("api key", "auth", "unauthorized", "forbidden", "configured")
    ):
        return "auth_or_config"
    # "rate" alone also matches "generate", "moderate" and "separate", which are ordinary
    # words in a model's error text. Only a stated rate limit counts as one.
    if "rate limit" in message or "rate_limit" in message or "429" in message:
        return "rate_limited"
    if isinstance(exc, (TimeoutError, requests.Timeout)) or "timeout" in message:
        return "timeout"
    if "context" in message and ("large" in message or "length" in message):
        return "context_too_large"
    if "unsupported" in message or "capability" in message:
        return "unsupported_capability"
    if isinstance(exc, (ConnectionError, requests.ConnectionError)) or any(
        marker in message for marker in _UNAVAILABLE_MARKERS
    ):
        # Nothing was served: the provider is not answering at all. That is an
        # availability fact the user can act on, not an internal fault in Neo.
        return "provider_unavailable"
    if isinstance(exc, requests.RequestException):
        return "transient_network"
    return "provider_error"


def user_message(category: str | None, provider_type: str | None = None) -> str:
    """The one sentence a failed provider request is allowed to show the user."""

    label = provider_label(provider_type)
    key = str(provider_type or "").strip().lower()
    if category == "provider_unavailable":
        return f"{label} is unavailable. {_RESTART_HINTS.get(key, _DEFAULT_RESTART_HINT)}"
    if category == "timeout":
        return (
            f"{label} did not respond in time. It may still be loading the model — "
            "try again in a moment."
        )
    if category == "transient_network":
        return f"Neo could not reach {label}. Check the connection, then try again."
    if category == "rate_limited":
        return f"{label} is rate limiting requests right now. Wait a moment, then try again."
    if category == "auth_or_config":
        return f"{label} is not set up correctly. Check the model settings, then try again."
    if category == "unsupported_capability":
        return f"{label} does not support this request. Choose a different model."
    return f"{label} could not finish this response. Try again."


def safe_error(exc: Exception) -> tuple[str, str, dict]:
    """Classify, and return the detailed text for the audit row and the logs.

    The second element is a debugging detail, not a user-facing one: it still names the
    provider's own error. Use :func:`user_message` for anything a person will read.
    """

    message, summary = safe_text(exc, 800)
    return classify(exc), message, summary
