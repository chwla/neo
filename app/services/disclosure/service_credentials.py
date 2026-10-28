"""Server-configured service keys, as capabilities bound to one origin.

A key the operator configured for a search provider answers a different question
from a disclosure label. The label says whether the *user's data* in a request may
go to a destination. The key says whether *this installation* may authenticate to
one particular service. Putting the key inside the labelled request made the
request's label describe the key as well -- and when that label was PUBLIC, it said
the key could go anywhere a public payload could.

So a key lives here instead: never in a labelled value, attached by the egress
boundary at the moment of transmission, and only when the request's origin is
exactly the one the key belongs to. A key is never permission to disclose anything;
the data in the request is authorized separately, by its own provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit


class CredentialScopeError(RuntimeError):
    """A credential would have been presented somewhere it does not belong."""


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` for ``url``, with the default port elided.

    Raises ``ValueError`` for anything that is not an absolute http(s) URL, or that
    carries embedded userinfo -- a URL like that is not one this module will bind a
    credential to, or present one at.
    """

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("an absolute http(s) URL is required")
    if parts.username is not None or parts.password is not None:
        raise ValueError("a URL with embedded credentials is not a valid origin")
    host = parts.hostname.rstrip(".").lower()
    port = parts.port
    default = 443 if parts.scheme == "https" else 80
    if port is None or port == default:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


@dataclass(frozen=True)
class ServiceCredential:
    """A server key, presented as one header to one exact origin."""

    name: str
    origin: str
    header_name: str
    _value: str = field(repr=False)
    _secret: str = field(repr=False, default="")

    @classmethod
    def header(
        cls, name: str, secret: str, *, origin: str, header: str, template: str = "{secret}"
    ) -> ServiceCredential:
        if not secret or not secret.strip():
            raise ValueError("a credential needs a non-empty secret")
        if not header or any(ch in header for ch in "\r\n:"):
            raise ValueError("invalid credential header name")
        normalized = origin_of(origin)
        if not normalized.startswith("https://"):
            # A key sent over plain HTTP is a key given to anyone on the path.
            raise ValueError("credentials are only presented to https origins")
        if urlsplit(origin).path not in {"", "/"}:
            raise ValueError("a credential origin has no path")
        return cls(name=name, origin=normalized, header_name=header,
                   _value=template.format(secret=secret), _secret=secret)

    # -- what egress needs -------------------------------------------------

    def admits(self, url: str) -> bool:
        try:
            return origin_of(url) == self.origin
        except ValueError:
            return False

    def secrets(self) -> tuple[str, ...]:
        """Strings that must never appear in the labelled payload."""

        # Both the raw key and the rendered header value: a caller that pasted the
        # bare key into a query string is as wrong as one that pasted the header.
        return tuple(value for value in (self._secret, self._value) if value)

    def header_value(self) -> str:
        return self._value

    # -- never print the secret --------------------------------------------

    def __repr__(self) -> str:
        return (
            f"ServiceCredential(name={self.name!r}, origin={self.origin!r}, "
            f"header={self.header_name!r}, secret=<redacted>)"
        )

    __str__ = __repr__


__all__ = ["CredentialScopeError", "ServiceCredential", "origin_of"]
