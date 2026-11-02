"""Where a key taken from the server's environment may be sent.

A model configuration names an environment variable for its key and a base URL
to send it to, and any signed-in session can edit both. Without a policy, that
let any profile -- or any guest -- point an operator's key at a server of their
choosing and collect it from the ``Authorization`` header. The rules here are
applied at the moment a key is attached to a request, not only when a
configuration is saved, so a configuration file edited by hand gets no further:

* the variable must be named like a provider key and not be one of Neo's own;
* a guest session never receives one;
* the URL must be ``https`` to a host on the allowlist for that variable --
  exact hostname, no IP literal, no userinfo, default port -- and requests that
  carry it do not follow redirects (see ``OpenAICompatibleClient``).

What this does not do, and the documentation says so: any non-guest profile can
still *spend* an operator key at the provider it belongs to, and any process
running as the same OS user can read the server's environment directly.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import active_profile_storage_dir, get_base_settings
from app.services.llm_registry.types import check_key_env_name

_LOG = logging.getLogger(__name__)

#: Each conventional variable and the one host its provider serves the API from.
#: ``NEO_PROVIDER_KEY_HOSTS`` adds to these (``MY_API_KEY=llm.example.com,...``).
DEFAULT_KEY_HOSTS: dict[str, frozenset[str]] = {
    "OPENAI_API_KEY": frozenset({"api.openai.com"}),
    "ANTHROPIC_API_KEY": frozenset({"api.anthropic.com"}),
    "GROQ_API_KEY": frozenset({"api.groq.com"}),
    "MISTRAL_API_KEY": frozenset({"api.mistral.ai"}),
    "OPENROUTER_API_KEY": frozenset({"openrouter.ai"}),
    "GEMINI_API_KEY": frozenset({"generativelanguage.googleapis.com"}),
    "GOOGLE_API_KEY": frozenset({"generativelanguage.googleapis.com"}),
    "DEEPSEEK_API_KEY": frozenset({"api.deepseek.com"}),
    "TOGETHER_API_KEY": frozenset({"api.together.xyz"}),
    "FIREWORKS_API_KEY": frozenset({"api.fireworks.ai"}),
    "XAI_API_KEY": frozenset({"api.x.ai"}),
    "PERPLEXITY_API_KEY": frozenset({"api.perplexity.ai"}),
    "CEREBRAS_API_KEY": frozenset({"api.cerebras.ai"}),
}


def _normalized_host(host: str) -> str:
    return host.encode("idna").decode("ascii").lower().rstrip(".")


def allowed_hosts(env_name: str) -> frozenset[str]:
    hosts = set(DEFAULT_KEY_HOSTS.get(env_name.upper(), frozenset()))
    configured = get_base_settings().provider_key_hosts or ""
    for entry in configured.split(","):
        name, _, host = entry.partition("=")
        if name.strip().upper() == env_name.upper() and host.strip():
            try:
                hosts.add(_normalized_host(host.strip()))
            except UnicodeError:
                continue
    return frozenset(hosts)


def url_refusal(env_name: str, base_url: str) -> str | None:
    """Why ``env_name``'s key may not go to ``base_url``, or ``None`` if it may."""

    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:
        return "the base URL is not valid"
    if parts.scheme != "https":
        return "a key from the server environment is only sent over https"
    if parts.username or parts.password:
        return "the base URL may not carry a user name or password"
    if not parts.hostname:
        return "the base URL has no host"
    if port not in (None, 443):
        return "a key from the server environment is only sent to the default https port"
    try:
        ipaddress.ip_address(parts.hostname)
        return "a key from the server environment is never sent to an IP address"
    except ValueError:
        pass
    try:
        host = _normalized_host(parts.hostname)
    except UnicodeError:
        return "the base URL's host name is not valid"
    if host not in allowed_hosts(env_name):
        return (
            f"{env_name} may only be sent to its provider's own host; add "
            f"{env_name}={host} to NEO_PROVIDER_KEY_HOSTS to allow this one"
        )
    return None


def _guest_session() -> bool:
    storage = active_profile_storage_dir.get()
    return bool(storage) and Path(storage).parent.name == "guests"


def refusal(env_name: str | None, base_url: str) -> str | None:
    """Why the server-environment key named ``env_name`` may not be used here."""

    if not env_name:
        return None
    try:
        check_key_env_name(env_name)
    except ValueError as exc:
        return str(exc)
    if _guest_session():
        return "guest sessions cannot use keys from the server environment"
    return url_refusal(env_name, base_url)


def env_key(env_name: str | None, base_url: str) -> str | None:
    """The key in ``env_name`` if policy allows sending it to ``base_url``, else ``None``."""

    if not env_name:
        return None
    reason = refusal(env_name, base_url)
    if reason is not None:
        _LOG.info("provider_env_key_withheld variable=%s reason=%s", env_name, reason)
        return None
    return os.getenv(env_name)
