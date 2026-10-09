"""Faoxima mirror hub pure helpers (MGMT-10..12): no database needed."""

from __future__ import annotations

from paurubot.bot.routers.manage import (
    HUB_SECTIONS,
    STUBBED_SECTIONS,
    TEST_CFG_DEFAULTS,
    Pulse,
    capacity_of,
    full_pulse_text,
    fx_effective,
    fx_hub_kb,
    fx_panels_kb,
    fx_stats_kb,
    hidden_ids,
    panel_renew_mode,
    parse_quick_price,
    quick_current,
    renewal_share,
    trial_cfg,
    with_hidden_added,
    with_hidden_removed,
)
from paurubot.core.settings import SPECS
from paurubot.i18n import Texts, catalog, fields

HUB_FA = [
    "آمار ربات 📊",
    "مدیریت پنل‌ها و سرورها 📁",
    "تنظیمات فروشگاه 🏬",
    "مالی و گزارشات 💎",
    "مدیریت کاربران 👥",
    "بخش پشتیبانی 🤙",
    "بخش آموزش 📚",
    "کانال و اطلاع‌رسانی 📢",
    "تنظیمات فنی و ربات ⚙️",
]
HOME_FA = "بازگشت به منوی اصلی 🏠"
STATS_FA = [
    "آمار کل ⏱️",
    "یک ساعت اخیر",
    "امروز ⛅️",
    "دیروز ☀️",
    "ماه فعلی",
    "ماه قبل",
    "مشاهده آمار در تاریخ مشخص 🗓",
    "بستن ❌",
]
PANELS_FA = [
    "مدیریت پنل ✏️",
    "اضافه کردن پنل 🖥",
    "قیمت سریع زمان ⏳",
    "قیمت سریع حجم 🔋",
]


def _buttons(kb) -> list[str]:  # type: ignore[no-untyped-def]
    return [button.text for row in kb.inline_keyboard for button in row]


def test_hub_has_nine_sections_with_exact_labels() -> None:
    assert len(HUB_SECTIONS) == 9
    assert len(STUBBED_SECTIONS) == 7
    assert {name for name, _ in HUB_SECTIONS} - STUBBED_SECTIONS == {"stats", "panels"}
    assert [Texts("fa")(key) for _, key in HUB_SECTIONS] == HUB_FA
    buttons = _buttons(fx_hub_kb(Texts("fa")))
    assert buttons[:9] == HUB_FA
    assert buttons[9] == HOME_FA


def test_stats_slices_with_exact_labels() -> None:
    buttons = _buttons(fx_stats_kb(Texts("fa"), "today"))
    assert [text.removeprefix("● ") for text in buttons] == STATS_FA
    assert buttons[2].startswith("● ")
    assert _buttons(fx_stats_kb(Texts("en"), "all"))[0].startswith("● ")


def test_panels_menu_with_exact_labels() -> None:
    buttons = _buttons(fx_panels_kb(Texts("fa")))
    assert buttons[:4] == PANELS_FA


def test_mirror_locales_match() -> None:
    fa = {key: catalog("fa")[key] for key in catalog("fa") if key.startswith("fx_")}
    en = {key: catalog("en")[key] for key in catalog("en") if key.startswith("fx_")}
    assert fa and set(fa) == set(en)
    for key in fa:
        assert fields(fa[key]) == fields(en[key]), key
    assert fields(fa["fx_stub"]) == {"name"}


def _pulse() -> Pulse:
    return Pulse(
        period="today",
        sales=4,
        sales_amount=400000,
        topups=2,
        topup_amount=100000,
        joined=5,
        customers=8,
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


def test_full_pulse_text_adds_averages() -> None:
    assert renewal_share(1, 4) == "25.0%"
    assert renewal_share(0, 0) == "0.0%"
    for locale in ("en", "fa"):
        text = full_pulse_text(Texts(locale), _pulse())
        assert "{" not in text and "}" not in text
        assert "50,000" in text  # 400000 // 8 per customer
        assert "25.0%" in text


def test_hidden_list_helpers() -> None:
    assert hidden_ids(None) == []
    assert hidden_ids({}) == []
    assert hidden_ids({"hide_user": "nope"}) == []
    assert hidden_ids({"hide_user": [7, "9", -1, 0, 7, True, "junk"]}) == [7, 9]
    added = with_hidden_added({"hide_user": [7]}, 9)
    assert added["hide_user"] == [7, 9]
    assert with_hidden_added(added, 9)["hide_user"] == [7, 9]
    assert with_hidden_added(None, 0)["hide_user"] == []
    assert with_hidden_removed({"hide_user": [7, 9]}, 7)["hide_user"] == [9]
    assert with_hidden_removed(None, 7)["hide_user"] == []


def test_trial_desk_defaults_and_merge() -> None:
    assert trial_cfg(None) == TEST_CFG_DEFAULTS
    assert trial_cfg({}) == TEST_CFG_DEFAULTS
    merged = trial_cfg({"test_cfg": {"enabled": False, "days": 3, "bogus": 1}})
    assert merged["enabled"] is False
    assert merged["days"] == 3
    assert merged["gb"] == TEST_CFG_DEFAULTS["gb"]
    assert set(merged) == set(TEST_CFG_DEFAULTS)
    broken = trial_cfg({"test_cfg": {"days": "junk"}})
    assert broken["days"] == TEST_CFG_DEFAULTS["days"]


def test_fx_math_and_quick_prices() -> None:
    assert fx_effective(1000, 0) == 1000
    assert fx_effective(1000, 10) == 1100
    assert fx_effective(0, 10) == 0
    assert fx_effective(-5, -5) == 0
    assert parse_quick_price("25000") == 25000
    assert parse_quick_price(None) is None
    assert parse_quick_price("junk") is None
    assert parse_quick_price("-1") is None
    assert parse_quick_price("100000001") is None
    assert quick_current(None) == "-"
    assert quick_current({}) == "- / - / -"
    assert quick_current({"tier1": 1, "tier2": 2, "tier3": 3}) == "1 / 2 / 3"


def test_panel_renew_mode_precedence() -> None:
    assert panel_renew_mode({"renew_resets_usage": True}, {}) == "extend_reset"
    assert panel_renew_mode({"renew_resets_usage": False}, {}) == "extend_carry"
    assert panel_renew_mode({}, {"renew_mode": "restart_carry"}) == "restart_carry"
    assert panel_renew_mode({}, {"renew_resets_usage": False}) == "extend_carry"
    assert panel_renew_mode(None, {}) == "extend_reset"


def test_capacity_helper() -> None:
    assert capacity_of(None) is None
    assert capacity_of({}) is None
    assert capacity_of({"capacity_limit": 0}) is None
    assert capacity_of({"capacity_limit": 25}) == 25
    assert capacity_of({"capacity_limit": "junk"}) is None


def test_quick_price_specs_exist() -> None:
    for key in ("quick_time_prices", "quick_volume_prices"):
        assert SPECS[key].kind is dict
        assert SPECS[key].default == {}
        assert SPECS[key].group == "services"
