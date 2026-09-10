"""What a provider is, and the registry of the ones Neo knows.

A provider is a declaration, not a client. It says where a provider's OAuth
endpoints are, which hosts it is allowed to be reached on, what each capability
costs in that provider's own scope vocabulary, and how to read an identity
response. It does not make HTTP requests -- that belongs to one module per
provider family, so there is exactly one place where a credential is attached to
a request and exactly one place an exception carrying one could escape from.

That division is what keeps the boundary honest. Everything provider-specific
(a URL, a scope string, an "access_type=offline" quirk) lives behind this
protocol; everything above it -- ``oauth.py``, ``store.py``, ``service.py``, the
routes and the settings screen -- is written once and works for the next
provider. Adding ``microsoft.py`` should touch no file outside this package,
and that is the acceptance test for the boundary rather than a hope about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class OAuthClient:
    """The application's own OAuth identity with a provider.

    ``client_secret`` is not a secret for the native-app clients Neo uses. RFC
    8252 section 8.5 is explicit that a client shipped to users cannot keep one,
    which is why PKCE and not confidentiality is what secures the flow, and why
    tools like gcloud and rclone ship theirs in the open. It is carried here
    because providers still require the value at the token endpoint, not because
    it protects anything.
    """

    client_id: str
    client_secret: str = ""


@dataclass(frozen=True)
class AccountIdentity:
    """Who the freshly authorised tokens belong to."""

    email: str
    subject: str


@runtime_checkable
class Provider(Protocol):
    id: str
    display_name: str
    authorization_url: str
    token_url: str
    revoke_url: str | None
    #: The exact hosts this provider may be reached on. The HTTP client refuses
    #: anything else before sending, which is a stronger guarantee than validating
    #: a URL's shape: these endpoints are constants, so an allowlist can be exact
    #: rather than merely "not obviously internal".
    allowed_hosts: frozenset[str]
    #: Where to ask who the token belongs to. Fetched by the provider's client;
    #: the response comes back here to ``parse_identity``.
    identity_url: str
    #: Capabilities this provider can actually offer. A capability in the shared
    #: vocabulary that a provider does not implement is simply absent, never
    #: silently mapped to something close.
    supported_capabilities: frozenset[str]

    def scopes_for(self, capability_ids: tuple[str, ...]) -> list[str]: ...

    def capabilities_from_scopes(
        self, granted_scopes: frozenset[str], requested: tuple[str, ...]
    ) -> tuple[str, ...]: ...

    def authorization_params(self) -> dict[str, str]: ...

    def default_client(self) -> OAuthClient | None: ...

    def parse_identity(self, payload: dict) -> AccountIdentity: ...

    def tier_for(self, capability_id: str) -> str: ...


class UnknownProviderError(ValueError):
    """A provider id nothing is registered under."""


def _registry() -> dict[str, Provider]:
    # Imported lazily so a provider module can import this one for its types
    # without the two forming a cycle at import time.
    from app.services.integrations.providers import google

    return {google.PROVIDER.id: google.PROVIDER}


def get_provider(provider_id: str) -> Provider:
    try:
        return _registry()[str(provider_id).strip().lower()]
    except KeyError as exc:
        raise UnknownProviderError(f"Unknown provider '{provider_id}'.") from exc


def list_providers() -> list[Provider]:
    return list(_registry().values())
