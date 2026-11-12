"""IPv6 addresses that carry an IPv4 address are judged by that address (audit SEC-08).

On a network with NAT64, `64:ff9b::a9fe:a9fe` reaches 169.254.169.254 (cloud metadata), and 6to4
(`2002::/16`) embeds an IPv4 address the same way. The outbound policy now unwraps them, as it
already did for IPv4-mapped addresses.
"""

from __future__ import annotations

import pytest

from paurubot.security import ssrf

pytestmark = pytest.mark.security


@pytest.mark.parametrize(
    "address",
    [
        "64:ff9b::a9fe:a9fe",  # NAT64 well-known prefix -> 169.254.169.254
        "64:ff9b:1::a9fe:a9fe",  # local-use NAT64 prefix
        "2002:a9fe:a9fe::1",  # 6to4 -> 169.254.169.254
    ],
)
def test_metadata_behind_an_ipv6_wrapper_is_blocked(address: str) -> None:
    with pytest.raises(ssrf.UnsafeUrl):
        ssrf.classify(address, allow_private=True)


@pytest.mark.parametrize("address", ["64:ff9b::a00:1", "2002:c0a8:101::1"])  # 10.0.0.1, 192.168.1.1
def test_private_networks_behind_a_wrapper_need_approval(address: str) -> None:
    with pytest.raises(ssrf.UnsafeUrl):
        ssrf.classify(address, allow_private=False)
    ssrf.classify(address, allow_private=True)


def test_public_addresses_behind_a_wrapper_pass() -> None:
    ssrf.classify("64:ff9b::808:808", allow_private=False)  # 8.8.8.8
    ssrf.classify("2606:4700:4700::1111", allow_private=False)
