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
    spec = g.parse_lottery("Draw | points | top | 30 | 10000 | top")
    assert spec == {
        "title": "Draw",
        "mode": "points",
        "pick": "top",
        "days": 30,
        "points_per": 10000,
        "board": "top",
    }
    plain = g.parse_lottery("Draw | join | random | 7")
    assert plain is not None and plain["points_per"] == 0 and plain["board"] == "none"
    assert g.parse_lottery("Draw | lucky | top | 7") is None
    assert g.parse_lottery("Draw | points | top | 7") is None  # points need toman per point
    assert g.parse_lottery("Draw | points | top | 7 | 0") is None
    assert g.parse_lottery("Draw | join | best | 7") is None
    assert g.parse_lottery("Draw | join | top | 400") is None
    assert g.parse_lottery("Draw | join | top | 7 | - | everyone") is None
    assert g.parse_lottery("Draw | join | top") is None


def test_parse_lottery_prize_in_both_languages() -> None:
    from paurubot.i18n import Texts

    fa, en = Texts("fa"), Texts("en")
    assert g.parse_lottery_prize(en, "gb 10") == {"kind": "gb", "value": 10, "label": ""}
    assert g.parse_lottery_prize(fa, fa("adm_grw_lot_kw_gb").split(",")[2] + " 10") == {
        "kind": "gb",
        "value": 10,
        "label": "",
    }
    assert g.parse_lottery_prize(fa, "wallet 10000 | first") == {
        "kind": "wallet",
        "value": 10000,
        "label": "first",
    }
    assert g.parse_lottery_prize(en, "days 5")["kind"] == "days"  # type: ignore[index]
    assert g.parse_lottery_prize(en, "text A special account") == {
        "kind": "text",
        "value": 0,
        "label": "A special account",
    }
    for bad in ("gb", "gb ten", "gold 10", "gb 0", "text", "days 99999", ""):
        assert g.parse_lottery_prize(en, bad) is None, bad


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
    keys += [f"adm_grw_mode_{mode}" for mode in g.growth.LOTTERY_MODES]
    keys += [f"adm_grw_pick_{pick}" for pick in g.growth.LOTTERY_PICKS]
    keys += [f"adm_grw_board_{board}" for board in g.growth.LOTTERY_BOARDS]
    keys += [f"adm_grw_lot_kw_{kind}" for kind in g.growth.LOTTERY_PRIZE_KINDS]
    statuses = ("paid", "queued", "applied", "failed", "no_service", "manual")
    keys += [f"report_lottery_st_{status}" for status in statuses]
    keys += [f"lottery_pick_{pick}" for pick in g.growth.LOTTERY_PICKS]
    for locale in ("fa", "en"):
        texts = {}
        for part in ("grw", "lottery"):
            texts |= json.loads((I18N / f"{locale}.{part}.json").read_text(encoding="utf-8"))
        missing = [key for key in keys if key not in texts]
        assert not missing, (locale, missing)
