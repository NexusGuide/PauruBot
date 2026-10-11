from __future__ import annotations

import json
from pathlib import Path

from paurubot.bot.routers import admin_extra as x
from paurubot.core import settings as shop_settings

I18N = Path(__file__).resolve().parents[2] / "paurubot" / "i18n"


def test_every_key_is_a_known_setting() -> None:
    assert x.KEYS and len(x.KEYS) == len(set(x.KEYS))
    assert all(key in shop_settings.SPECS for key in x.KEYS)
    assert all(key in x.KEYS for key in x.BOUNDS)


def test_parse_extra() -> None:
    assert x.parse_extra("referral_percent", "15") == (15, True)
    assert x.parse_extra("referral_percent", "150")[1] is False
    assert x.parse_extra("referral_percent", "abc")[1] is False
    assert x.parse_extra("wallet_transfer_min", "-") == (10_000, True)
    assert x.parse_extra("referral_repeat_percent", "-") == (None, True)


def test_pair_ok() -> None:
    cfg = {"custom_min_gb": 10, "custom_max_gb": 500, "custom_min_days": 7, "custom_max_days": 180}
    assert x.pair_ok("custom_min_gb", 100, cfg)
    assert not x.pair_ok("custom_min_gb", 600, cfg)
    assert not x.pair_ok("custom_max_days", 5, cfg)
    assert x.pair_ok("referral_percent", 5, cfg)


def test_can_turn_on() -> None:
    assert not x.can_turn_on("custom_buy_enabled", {"custom_panel_id": None})
    assert x.can_turn_on("custom_buy_enabled", {"custom_panel_id": 3})
    assert x.can_turn_on("miniapp_enabled", {})


def test_dynamic_labels_exist_in_both_locales() -> None:
    keys = [f"adm_x_{key}" for key in x.KEYS] + [f"adm_x_group_{name}" for name in x.GROUPS]
    for locale in ("fa", "en"):
        texts = json.loads((I18N / f"{locale}.xcfg.json").read_text(encoding="utf-8"))
        missing = [key for key in keys if key not in texts]
        assert not missing, (locale, missing)
