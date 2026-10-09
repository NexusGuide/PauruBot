"""Command deck pure helpers (MGMT-01..03): no database needed."""

from __future__ import annotations

from datetime import UTC, datetime

from paurubot.bot.routers.manage import (
    SHOWCASE_DEFAULTS,
    Pulse,
    format_ping,
    parse_custom_date,
    pulse_text,
    showcase_flags,
    slice_last_month,
)
from paurubot.db.models import Panel, PanelKind
from paurubot.i18n import Texts
from paurubot.panels.registry import visible_in_shop


def test_slice_last_month_bounds() -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    start, end = slice_last_month(now)
    assert (start.year, start.month, start.day) == (2026, 9, 1)
    assert (end.year, end.month, end.day) == (2026, 10, 1)
    assert (end - start).days == 30
    assert start.tzinfo is not None and end.tzinfo is not None


def test_slice_last_month_year_edge() -> None:
    now = datetime(2026, 1, 15, 8, 30, tzinfo=UTC)
    start, end = slice_last_month(now)
    assert (start.year, start.month, start.day) == (2025, 12, 1)
    assert (end.year, end.month, end.day) == (2026, 1, 1)


def test_parse_custom_date_valid() -> None:
    assert parse_custom_date("2026-10-05") == datetime(2026, 10, 5, tzinfo=UTC)
    assert parse_custom_date(" 2026-01-01 ") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_custom_date_invalid() -> None:
    assert parse_custom_date(None) is None
    assert parse_custom_date("") is None
    assert parse_custom_date("not-a-date") is None
    assert parse_custom_date("2026-02-30") is None
    assert parse_custom_date("2026-13-01") is None
    assert parse_custom_date("1405/07/14") is None
    assert parse_custom_date("2026/10/05") is None
    assert parse_custom_date("26-10-05") is None


def test_showcase_flags_defaults_and_merge() -> None:
    assert showcase_flags(None) == SHOWCASE_DEFAULTS
    assert showcase_flags({}) == SHOWCASE_DEFAULTS
    assert showcase_flags({"showcase": "nope"}) == SHOWCASE_DEFAULTS
    merged = showcase_flags({"showcase": {"show_in_shop": False, "direct_buy": True}})
    assert merged["show_in_shop"] is False
    assert merged["direct_buy"] is True
    assert merged["allow_renew"] is True
    assert set(merged) == set(SHOWCASE_DEFAULTS)


def _panel(options: dict | None) -> Panel:
    return Panel(
        shop_id=1,
        name="deck-panel",
        kind=PanelKind.MARZBAN,
        base_url="https://panel.example.com",
        username="admin",
        password_enc="enc",
        options=options or {},
    )


def test_visible_in_shop_helper() -> None:
    assert visible_in_shop(_panel(None)) is True
    assert visible_in_shop(_panel({})) is True
    assert visible_in_shop(_panel({"showcase": {"show_in_shop": False}})) is False
    assert visible_in_shop(_panel({"showcase": {"direct_buy": True}})) is True
    assert visible_in_shop(_panel({"showcase": "nope"})) is True


def test_format_ping() -> None:
    assert format_ping(None) == "-"
    assert format_ping(0) == "0 ms"
    assert format_ping(42) == "42 ms"


def _pulse() -> Pulse:
    return Pulse(
        period="today",
        sales=3,
        sales_amount=250000,
        topups=2,
        topup_amount=100000,
        joined=5,
        customers=11,
        buyers=2,
        conversion="40.0%",
        tests=1,
        active=7,
        renewals=1,
        forecast=7500000,
        panels=2,
        healthy=1,
        res_l1=1,
        res_l2=0,
        wallet=500000,
        bot="12 ms",
        db="3 ms",
    )


def test_pulse_text_renders_with_fake_data() -> None:
    for locale in ("en", "fa"):
        text = pulse_text(Texts(locale), _pulse())
        assert "{" not in text and "}" not in text
        for figure in ("3", "250,000", "100,000", "11", "40.0%", "7,500,000", "12 ms"):
            assert figure in text, (locale, figure)
