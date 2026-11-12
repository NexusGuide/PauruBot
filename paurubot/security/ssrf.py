"""Outbound URL policy for panel and gateway calls (SEC-10)."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALWAYS_BLOCKED = [
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "169.254.0.0/16",  # link-local, cloud metadata
        "100.64.0.0/10",  # carrier-grade NAT
        "192.0.0.0/24",
        "198.18.0.0/15",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "fe80::/10",
        "ff00::/8",
        "fd00:ec2::254/128",
    )
]
PRIVATE = [
    ipaddress.ip_network(net)
    for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128", "fc00::/7")
]


class UnsafeUrl(ValueError):
    pass


def check_url_shape(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise UnsafeUrl("only http and https URLs are allowed")
    if not parts.hostname:
        raise UnsafeUrl("URL has no host")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials inside URLs are not allowed")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme, parts.hostname, port


LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def check_static(url: str, *, allow_private: bool = False) -> None:
    """Checks that need no DNS: used when an address is saved. Calls are still guarded at connect time."""
    _, host, _ = check_url_shape(url)
    name = host.lower().rstrip(".")
    if name in LOCAL_NAMES or name.endswith(".localhost"):
        if not allow_private:
            raise UnsafeUrl(f"{host} is a local name; approve private addresses for this panel first")
        return
    try:
        address = str(ipaddress.ip_address(name))
    except ValueError:
        return
    classify(address, allow_private=allow_private)


# IPv6 prefixes that carry an IPv4 address in their low 32 bits (NAT64, RFC 6052 / 8215).
NAT64 = [ipaddress.ip_network(net) for net in ("64:ff9b::/96", "64:ff9b:1::/48")]


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 one stands for: mapped, 6to4 or NAT64 (audit SEC-08)."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if any(ip in net for net in NAT64):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def classify(address: str, *, allow_private: bool) -> None:
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        ip = _embedded_ipv4(ip) or ip
    for net in ALWAYS_BLOCKED:
        if ip in net:
            raise UnsafeUrl(f"address {ip} is in a blocked range")
    if not allow_private:
        for net in PRIVATE:
            if ip in net:
                raise UnsafeUrl(f"address {ip} is private; approve private addresses for this panel first")


async def resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"cannot resolve {host}") from exc
    return sorted({str(info[4][0]) for info in infos})


async def assert_safe_url(url: str, *, allow_private: bool = False) -> list[str]:
    """Validate the URL and every address its host resolves to. Returns the addresses."""
    _, host, port = check_url_shape(url)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = await resolve(host, port)
    if not addresses:
        raise UnsafeUrl(f"{host} resolved to no addresses")
    for address in addresses:
        classify(address, allow_private=allow_private)
    return addresses
