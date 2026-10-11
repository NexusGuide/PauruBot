from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from paurubot.bot.routers import admin_config
from paurubot.bot.routers.admin_config import (
    CONFIG,
    CONFIG_KEYS,
    config_buttons,
    parse_card,
    parse_code,
    parse_value,
    shown,
)
from paurubot.core import settings as shop_settings

I18N = Path(admin_config.__file__).resolve().parents[2] / "i18n"


def _locale(name: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for path in sorted(I18N.glob(f"{name}.*.json")):
        data.update(json.loads(path.read_text(encoding="utf-8")))
    return data


class FakeStaff:
    def __init__(self, *perms: str) -> None:
        self.perms = set(perms)

    def can(self, perm: str) -> bool:
        return perm in self.perms


def _t(key: str, **_: Any) -> str:
    return key


def test_every_config_key_is_a_setting_with_labels() -> None:
    assert CONFIG_KEYS
    for locale in ("fa", "en"):
        texts = _locale(locale)
        for group, keys in CONFIG.items():
            assert f"adm_cfg_group_{group}" in texts
            for key in keys:
                assert key in shop_settings.SPECS
                assert f"adm_cfg_{key}" in texts, (locale, key)


def test_parse_value() -> None:
    assert parse_value("stars_rate", "2500") == (2500, None)
    assert parse_value("daily_report_hour", "24")[1] == "adm_cfg_range"
    assert parse_value("stars_rate", "abc")[1] == "adm_cfg_range"
    assert parse_value("stars_rate", "-") == (2_000, None)
    assert parse_value("zarinpal_sandbox", "on") == (True, None)
    assert parse_value("zarinpal_sandbox", "maybe")[1] == "adm_cfg_invalid"
    assert parse_value("abangateway_url", "http://x.example")[1] == "adm_cfg_invalid"
    assert parse_value("abangateway_url", "https://x.example") == ("https://x.example", None)
    assert parse_value("card_pick", "rotate") == ("rotate", None)
    assert parse_value("card_pick", "nope")[1] == "adm_cfg_invalid"
    assert parse_value("tron_address", "-") == ("", None)
    assert parse_value("tron_address", "a\nb")[1] == "adm_cfg_invalid"


def test_secrets_are_never_shown() -> None:
    assert shown("zarinpal_merchant", "abcd-1234") == "\u2705"
    assert shown("zarinpal_merchant", "") == "\u2014"
    assert shown("stars_rate", 10) == "10"


def test_parse_code() -> None:
    assert parse_code("OFF20 20% 100 30", percent_ok=True) == {
        "code": "OFF20",
        "value": 20,
        "percent": True,
        "uses": 100,
        "days": 30,
    }
    fixed = parse_code("OFF50K 50000", percent_ok=True)
    assert fixed is not None and not fixed["percent"] and fixed["uses"] is None
    random = parse_code("* 20000 0 0", percent_ok=False)
    assert random is not None and len(random["code"]) == 8 and random["days"] is None
    assert parse_code("GIFT 20% 1", percent_ok=False) is None
    assert parse_code("OFF 150%", percent_ok=True) is None
    assert parse_code("OFF 500", percent_ok=True) is None
    assert parse_code("a 5000", percent_ok=True) is None
    assert parse_code("OFF 5000 -1", percent_ok=True) is None
    assert parse_code("OFF", percent_ok=True) is None


def test_parse_card() -> None:
    card = parse_card("4111 1111 1111 1111 | Ali | Melli")
    assert card == {"number": "4111111111111111", "holder": "Ali", "bank": "Melli"}
    assert parse_card("4111111111111112") is None
    assert parse_card("1234") is None


@pytest.mark.parametrize(
    ("perms", "sections"),
    [
        ((), []),
        (("settings.write",), ["cfg", "cards"]),
        (("catalog.write", "wallet.adjust"), ["dc", "gc"]),
        (("panels.write",), ["pcfg"]),
    ],
)
def test_config_buttons_follow_permissions(perms: tuple[str, ...], sections: list[str]) -> None:
    buttons = config_buttons(_t, FakeStaff(*perms))  # type: ignore[arg-type]
    assert [target.section for _, target in buttons] == sections
