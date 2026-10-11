from __future__ import annotations

import json
from pathlib import Path

from paurubot.bot.routers import admin_growth as g

I18N = Path(__file__).resolve().parents[2] / "paurubot" / "i18n"


def test_parse_prize_accepts_valid_slices() -> None:
    assert g.parse_prize("Cash | wallet | 10000 | 30") == {
        "label": "Cash",
        "kind": "wallet",
        "value": 10000,
        "weight": 30,
    }
    assert g.parse_prize("Off | DISCOUNT | 10 | 5")["kind"] == "discount"


def test_parse_prize_rejects_bad_input() -> None:
    assert g.parse_prize("only | three | parts") is None
    assert g.parse_prize("x | gold | 1 | 1") is None
    assert g.parse_prize("x | discount | 150 | 1") is None
    assert g.parse_prize("x | wallet | 10 | 0") is None
    assert g.parse_prize("x | wallet | abc | 1") is None


def test_parse_lottery() -> None:
    spec = g.parse_lottery("Draw | 50GB | 0 | 3 | join | 7")
    assert spec is not None and spec["winners"] == 3 and spec["days"] == 7 and spec["mode"] == "join"
    assert g.parse_lottery("Draw | 50GB | 0 | 3 | lucky | 7") is None
    assert g.parse_lottery("Draw | 50GB | 0 | 0 | join | 7") is None
    assert g.parse_lottery("Draw | 50GB | 0 | 3 | join | 400") is None
    assert g.parse_lottery("Draw | 50GB | 0 | 3 | join") is None


def test_parse_ask() -> None:
    assert g.parse_ask("wheel_every_hours", "12") == (12, True)
    assert g.parse_ask("wheel_every_hours", "0")[1] is False
    assert g.parse_ask("wheel_every_hours", "9999")[1] is False
    assert g.parse_ask("trust_channel_id", "-1001234567890") == (-1001234567890, True)
    assert g.parse_ask("trust_channel_id", "-") == (None, True)
    assert g.parse_ask("trust_channel_id", "@channel")[1] is False


def test_dynamic_labels_exist_in_both_locales() -> None:
    keys = [f"adm_grw_{key}" for key in (*g.TOGGLES, *g.ASKS)]
    keys += [f"adm_grw_kind_{kind}" for kind in ("wallet", "discount", "nothing")]
    keys += [f"adm_grw_status_{status}" for status in ("open", "drawn", "canceled")]
    keys += [f"adm_grw_mode_{mode}" for mode in g.MODES]
    for locale in ("fa", "en"):
        texts = json.loads((I18N / f"{locale}.grw.json").read_text(encoding="utf-8"))
        missing = [key for key in keys if key not in texts]
        assert not missing, (locale, missing)
