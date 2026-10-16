"""Validation for reseller bot customization inputs."""

from paurubot.core.resale_customization import parse_channel


def test_forced_channel_parser_accepts_username_and_numeric_chat() -> None:
    assert parse_channel("@my_vpn | https://t.me/my_vpn") == ("@my_vpn", "https://t.me/my_vpn")
    assert parse_channel("-100123456 | https://t.me/+invite") == ("-100123456", "https://t.me/+invite")


def test_forced_channel_parser_rejects_unsafe_values() -> None:
    assert parse_channel("@x | http://t.me/x") is None
    assert parse_channel("@x | https://example.com/x") is None
    assert parse_channel("not a channel") is None
