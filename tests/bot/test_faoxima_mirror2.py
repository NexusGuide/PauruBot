"""Wave-2 mirror desks: pure helpers (no database)."""

from __future__ import annotations

from paurubot.bot.routers.manage import (
    W2_CARD_BOOL_FIELDS,
    W2_CARD_INT_FIELDS,
    W2_GATEWAYS,
    W2_TECH_TOGGLES,
    w2_card_accounts,
    w2_card_without,
    w2_on,
    w2_secret_present,
    w2_support_departments,
    w2_with_department,
    w2_without_department,
)


def test_w2_on_marks() -> None:
    assert w2_on(True) == "✅"
    assert w2_on(False) == "❌"
    assert w2_on(0) == "❌"
    assert w2_on("x") == "✅"


def test_support_departments_merge() -> None:
    assert w2_support_departments({}) == []
    assert w2_support_departments({"support_departments": "nope"}) == []
    cfg = {"support_departments": ["sales", "", "  ", "billing", "sales"]}
    assert w2_with_department(cfg, "abuse") == ["sales", "billing", "abuse"]
    assert w2_with_department(cfg, "sales") == ["sales", "billing"]
    assert w2_without_department(cfg, "sales") == ["billing"]


def test_card_accounts_parse() -> None:
    cfg = {
        "card_accounts": [
            {"number": "6037-9911-1234-5678", "owner": "Ali"},
            {"number": "short", "owner": "No"},
            "junk",
            {"number": " 6273 5312 0000 1111 ", "owner": ""},
        ]
    }
    rows = w2_card_accounts(cfg)
    assert [r["number"] for r in rows] == ["6037991112345678", "6273531200001111"]
    assert w2_card_without(rows, 0) == [rows[1]]
    assert w2_card_without(rows, 9) == rows
    assert w2_card_accounts({}) == []


def test_secret_presence() -> None:
    assert w2_secret_present({"a": "x"}, "a") is True
    assert w2_secret_present({"a": ""}, "a") is False
    assert w2_secret_present({}, "a") is False


def test_gateway_table_shape() -> None:
    keys = [row[0] for row in W2_GATEWAYS]
    assert keys[0] == "card" and len(keys) == len(set(keys)) == 15
    for _key, enabled, _min, _max in W2_GATEWAYS:
        assert enabled.endswith("_enabled")


def test_card_field_tables() -> None:
    assert len(W2_CARD_BOOL_FIELDS) == 6
    assert len(W2_CARD_INT_FIELDS) == 7
    for _spec, _label, lo, hi in W2_CARD_INT_FIELDS:
        assert 0 <= lo <= hi
    assert len(W2_TECH_TOGGLES) == 9
