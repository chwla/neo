from __future__ import annotations

import ipaddress
import socket
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests

MAX_CONNECTOR_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 15.0
# RFC 6598 carrier-grade NAT. Python's is_private returns False for this range, so it
# has to be listed explicitly: it is not routable on the public internet and is used
# for ISP CGNAT and internal cloud/container addressing, making it a pivot target.
SHARED_ADDRESS_SPACE = (ipaddress.ip_network("100.64.0.0/10"),)


class ConnectorSecurityError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# DNS-rebinding defence: pin the addresses validation approved
# --------------------------------------------------------------------------- #
#
# Validation resolved a hostname and then handed the *hostname* to requests, which
# resolved it again at connect time. Nothing tied the two together, so a short-TTL
# attacker record could answer "public" for validation and "private" for the
# connection. Pinning by address rather than rewriting the URL to an IP keeps the
# hostname in the URL, so TLS SNI, certificate verification and Host all still work.

_system_getaddrinfo = socket.getaddrinfo
_pins = threading.local()


def _active_pins() -> dict:
    pins = getattr(_pins, "map", None)
    if pins is None:
        pins = {}
        _pins.map = pins
    return pins


def _pinned_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):  # noqa: A002
    """Resolve through the thread's pin map, falling back to the system resolver."""

    addresses = _active_pins().get(str(host).rstrip(".").lower())
    if addresses is None:
        return _system_getaddrinfo(host, port, family, type, proto, flags)

    results = []
    for address in addresses:
        af = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        if family not in (0, socket.AF_UNSPEC) and family != af:
            continue
        sockaddr = (
            (str(address), port or 0)
            if af == socket.AF_INET
            else (str(address), port or 0, 0, 0)
        )
        results.append(
            (af, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", sockaddr)
        )
    if not results:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return results


if socket.getaddrinfo is not _pinned_getaddrinfo:
    socket.getaddrinfo = _pinned_getaddrinfo


@contextmanager
def pin_addresses(hostname: str, addresses):
    """Restrict resolution of ``hostname`` to ``addresses`` for the calling thread only.

    Thread-local by design: a pin taken for one in-flight request must not change how
    any other thread resolves the same name.
    """

    key = str(hostname).rstrip(".").lower()
    pins = _active_pins()
    if not key or not addresses:
        yield
        return
    previous = pins.get(key)
    pins[key] = list(addresses)
    try:
        yield
    finally:
        if previous is None:
            pins.pop(key, None)
        else:
            pins[key] = previous


@dataclass(frozen=True)
class SafeResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8"))


def _is_unsafe_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_multicast,
            address.is_unspecified,
            any(address in network for network in SHARED_ADDRESS_SPACE),
        )
    )


def _resolved_addresses(hostname: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        return list(
            {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        )
    except (OSError, ValueError) as exc:
        raise ConnectorSecurityError("Connector hostname could not be resolved.") from exc


def validate_connector_url(
    url: str,
    *,
    allow_trusted_localhost: bool = False,
    resolve: bool = True,
) -> str:
    """Validate a connector URL immediately before every request.

    Public connectors must use HTTPS. Plain HTTP and non-public addresses are
    accepted only for an explicitly trusted loopback connector.
    """

    return _validate_connector_url(
        url, allow_trusted_localhost=allow_trusted_localhost, resolve=resolve
    )[0]


def _validate_connector_url(
    url: str,
    *,
    allow_trusted_localhost: bool = False,
    resolve: bool = True,
) -> tuple[str, list]:
    """Validate, and also return the addresses approved, so they can be pinned."""

    parsed = urlparse(str(url).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConnectorSecurityError("Connector URL must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ConnectorSecurityError("Credentials must not be embedded in connector URLs.")
    if parsed.fragment:
        raise ConnectorSecurityError("Connector URLs must not contain fragments.")

    hostname = parsed.hostname.rstrip(".").lower()
    localhost_name = hostname in {"localhost", "ip6-localhost"} or hostname.endswith(".localhost")
    try:
        literal_address = ipaddress.ip_address(hostname)
        addresses = [literal_address]
    except ValueError:
        if localhost_name:
            addresses = (
                _resolved_addresses(hostname) if resolve else [ipaddress.ip_address("127.0.0.1")]
            )
        elif hostname.endswith((".local", ".internal", ".lan", ".intranet")):
            raise ConnectorSecurityError("Connector URL uses a blocked local hostname.") from None
        else:
            addresses = _resolved_addresses(hostname) if resolve else []

    if resolve and not addresses:
        raise ConnectorSecurityError("Connector hostname did not resolve to an address.")
    unsafe = any(_is_unsafe_address(address) for address in addresses)
    loopback_only = all(address.is_loopback for address in addresses)

    if unsafe:
        if (
            not allow_trusted_localhost
            or not (localhost_name or loopback_only)
            or not loopback_only
        ):
            raise ConnectorSecurityError("Connector URL resolves to a blocked private address.")
    elif parsed.scheme != "https":
        raise ConnectorSecurityError("Public connector URLs must use HTTPS.")

    return parsed.geturl(), addresses


def safe_request(
    method: str,
    url: str,
    *,
    allow_trusted_localhost: bool = False,
    headers: dict[str, str] | None = None,
    params: dict[str, object] | None = None,
    json_body: object | None = None,
    data: dict[str, str] | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_bytes: int = MAX_CONNECTOR_RESPONSE_BYTES,
) -> SafeResponse:
    current = validate_connector_url(url, allow_trusted_localhost=allow_trusted_localhost)
    request_method = method.upper()
    body = json_body
    form = data

    for _ in range(MAX_REDIRECTS + 1):
        _, approved = _validate_connector_url(
            current, allow_trusted_localhost=allow_trusted_localhost
        )
        pinned_host = (urlparse(current).hostname or "").rstrip(".").lower()
        try:
            # The connection is opened inside the pin, so it can only reach an address
            # validation just approved -- a rebound record is never consulted.
            with pin_addresses(pinned_host, approved), requests.Session() as session:
                session.trust_env = False
                response = session.request(
                    request_method,
                    current,
                    headers=headers or {},
                    params=params,
                    json=body,
                    data=form,
                    timeout=timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
                # The session can close after the streamed body is fully read
                # below; keep the response handling inside this iteration.
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    response.close()
                    if not location:
                        raise ConnectorSecurityError("Connector returned an invalid redirect.")
                    redirected = urljoin(current, location)
                    old = urlparse(current)
                    new = urlparse(redirected)
                    if (old.scheme, old.hostname, old.port) != (
                        new.scheme,
                        new.hostname,
                        new.port,
                    ):
                        raise ConnectorSecurityError(
                            "Cross-origin connector redirects are blocked."
                        )
                    current = redirected
                    if response.status_code in {301, 302, 303} and request_method not in {
                        "GET",
                        "HEAD",
                    }:
                        request_method, body, form = "GET", None, None
                    continue

                collected = bytearray()
                try:
                    for chunk in response.iter_content(16_384):
                        if not chunk:
                            continue
                        collected.extend(chunk)
                        if len(collected) > max_bytes:
                            raise ConnectorSecurityError(
                                "Connector response exceeded the size limit."
                            )
                finally:
                    response.close()
                return SafeResponse(
                    url=current,
                    status_code=response.status_code,
                    headers={
                        str(key).lower(): str(value) for key, value in response.headers.items()
                    },
                    body=bytes(collected),
                )
        except requests.Timeout as exc:
            raise ConnectorSecurityError("Connector request timed out.") from exc
        except requests.RequestException as exc:
            raise ConnectorSecurityError("Connector request failed.") from exc

    raise ConnectorSecurityError("Connector returned too many redirects.")
