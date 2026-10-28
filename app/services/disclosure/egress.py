"""Explicit labelled HTTP boundary for third-party outbound requests.

The complete request (including URL, headers, query parameters and body) is the
payload being authorized. Checking only a query while sending a separately built
request would leave a gap between the approval and the bytes transmitted.
Callers must retain provenance while constructing this envelope; this module
never infers PUBLIC from a URL or from the absence of a private-data match.

Configured internal model transports remain a separate authorized channel.
Legacy third-party callers are being migrated to this interface; importing this
module alone does not enforce the boundary on those callers.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import requests

from app.services.disclosure.gate import Approval, Destination, GrantStore, require
from app.services.disclosure.labels import Labeled, label_of
from app.services.disclosure.service_credentials import CredentialScopeError, ServiceCredential

_FIELDS = frozenset({"method", "url", "params", "headers", "json", "data"})
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


class EgressTransportError(RuntimeError):
    """A transport failure with no URL, credentials or response body in its text."""


class EgressTimeoutError(EgressTransportError):
    """A timeout that carries no private transport details."""


def snapshot(payload: Labeled) -> Labeled:
    """Freeze JSON-compatible request values before authorization and transmission."""
    if not isinstance(payload, Labeled) or not isinstance(payload.value, dict):
        raise ValueError("An explicitly labelled HTTP request is required")
    try:
        value = json.loads(json.dumps(payload.value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("HTTP request must contain only JSON-compatible values") from exc
    if not set(value) <= _FIELDS or not {"method", "url"} <= set(value):
        raise ValueError("Invalid HTTP request fields")
    if value["method"] not in _METHODS or not isinstance(value["url"], str):
        raise ValueError("Invalid HTTP request method or URL")
    try:
        url = urlsplit(value["url"])
        valid = url.scheme in {"http", "https"} and bool(url.hostname) and url.port != 0
    except ValueError:
        valid = False
    if not valid or url.username is not None or url.password is not None or url.fragment:
        raise ValueError("HTTP requests require an absolute URL without embedded credentials")
    headers = value.get("headers")
    if headers is not None and (
        not isinstance(headers, dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items())
    ):
        raise ValueError("Invalid HTTP request headers")
    return Labeled(value, label_of(payload))


def _check_credential_scope(frozen: Labeled, credential: ServiceCredential) -> None:
    """Refuse before anything is sent if the key would go astray.

    Checked ahead of the disclosure gate as well as the network: a refused request
    should not reach any decision point holding the key.
    """

    if not credential.admits(frozen.value["url"]):
        raise CredentialScopeError(
            f"the {credential.name} credential is bound to {credential.origin}, "
            "not to the requested origin"
        )
    headers = frozen.value.get("headers") or {}
    wanted = credential.header_name.lower()
    if any(name.lower() == wanted for name in headers):
        raise CredentialScopeError(
            f"the request already sets {credential.header_name}; it is reserved for the "
            "credential"
        )
    serialized = json.dumps(frozen.value)
    if any(secret in serialized for secret in credential.secrets()):
        raise CredentialScopeError(
            "a credential appeared inside the labelled request; keys travel only as "
            "capabilities"
        )


def request(
    payload: Labeled,
    destination: Destination,
    *,
    grants: GrantStore | None = None,
    approval: Approval | None = None,
    timeout: float = 30,
    stream: bool = False,
    credential: ServiceCredential | None = None,
) -> Labeled:
    """Send exactly one authorized request, returning explicitly labelled output.

    Redirects require a new labelled request and authorization. Each call uses a
    fresh session without ambient proxies, netrc credentials or cookies, so the
    approved envelope is not silently augmented with another account's state.
    SSRF/address pinning remains the caller's independent network-policy guard.

    ``credential`` is a server key, kept out of the labelled envelope entirely. The
    gate authorizes the data; the credential is attached afterwards, and only when
    the request's origin is exactly the one the key is bound to. A configured key
    therefore never makes a private payload sendable, and a public payload never
    makes a key sendable somewhere else.
    """
    frozen = snapshot(payload)
    if credential is not None:
        _check_credential_scope(frozen, credential)
    require("http", frozen, destination, grants=grants, approval=approval)
    send = dict(frozen.value)
    if credential is not None:
        send["headers"] = {**(send.get("headers") or {}),
                           credential.header_name: credential.header_value()}
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.request(
            **send, timeout=timeout, stream=stream, allow_redirects=False,
        )
    except requests.Timeout:
        session.close()
        raise EgressTimeoutError("Outbound HTTP request timed out") from None
    except requests.RequestException:
        session.close()
        raise EgressTransportError("Outbound HTTP request failed") from None
    # Session.close releases its pool but does not consume a streaming response.
    # The caller still owns the response and must close it after reading.
    session.close()
    return Labeled(response, frozen.label)
