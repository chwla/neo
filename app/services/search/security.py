from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def is_private_address(address: ipaddress._BaseAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or any(address in network for network in PRIVATE_NETWORKS)
    )


def resolve_hostname_ips(hostname: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    resolved: list[ipaddress._BaseAddress] = []
    for info in infos:
        try:
            resolved.append(ipaddress.ip_address(info[4][0]))
        except Exception:
            continue
    return resolved


def is_public_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        host = (parsed.hostname or "").strip()
        if not host:
            return False
        lower = host.lower()
        if lower in {"localhost", "metadata", "metadata.google.internal"}:
            return False
        if lower.endswith((".local", ".localhost", ".internal", ".lan", ".intranet")):
            return False
        try:
            return not is_private_address(ipaddress.ip_address(host))
        except ValueError:
            pass
        addresses = resolve_hostname_ips(host)
        return bool(addresses) and not any(is_private_address(address) for address in addresses)
    except Exception:
        return False
