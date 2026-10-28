"""Ports for local services that expose Neo data outside its session middleware."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.core.config import get_settings


def sensitive_ports() -> tuple[int, ...]:
    """Keep these denied for every untrusted process, not just external CLIs."""

    settings = get_settings()
    # Old defaults stay blocked even after a setting changes: the prior local
    # service may still be running. Qdrant archive payloads contain actual text.
    ports = {int(settings.port), 8000, 6333, 11434, 8080}
    for value in (
        settings.qdrant_url,
        settings.ollama_url,
        settings.searxng_instance,
        settings.openai_compat_base_url,
    ):
        try:
            parsed = urlsplit(value)
            if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                ports.add(parsed.port or (443 if parsed.scheme == "https" else 80))
        except ValueError:
            continue
    return tuple(sorted(ports))


def private_endpoint_refusal() -> str | None:
    """Refuse external engines when the private archive cannot be blocked by port.

    Seatbelt's network rule can deny a port but cannot safely deny one remote
    HTTPS host while leaving vendor HTTPS available. A remote vector archive is
    therefore an unsupported external-engine configuration for now.
    """

    try:
        endpoint = urlsplit(get_settings().qdrant_url)
    except ValueError:
        return "Qdrant has an invalid URL, so its private archive cannot be isolated"
    if endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return "Qdrant uses a remote host, so its private archive cannot be isolated"
    return None


__all__ = ["private_endpoint_refusal", "sensitive_ports"]
