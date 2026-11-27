"""Unit tests for Section 6 bugfixes and features."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from paurubot.api.routes.admin.system import EXPORT_PERMISSIONS, _csv_safe
from paurubot.core import business, exports, forced_join, reports
from paurubot.db.models import LedgerAccountKind, OrderKind


def test_growth_leaderboard_uses_valid_wallet_enum() -> None:
    from paurubot.api.routes.admin import growth

    src = inspect.getsource(growth.referral_leaderboard)
    assert "LedgerAccountKind.CUSTOMER_WALLET" not in src
    assert "LedgerAccountKind.WALLET" in src
    assert hasattr(LedgerAccountKind, "WALLET")


def test_exports_safe_guards_leading_whitespace_and_lists() -> None:
    assert exports.safe("=CMD|'/C calc'!A0") == "'=CMD|'/C calc'!A0"
    assert exports.safe("  =SUM(A1:A2)") == "'  =SUM(A1:A2)"
    assert exports.safe("\t+123") == "'\t+123"
    assert exports.safe(["=HYPERLINK(1)", "vip"]) == "'=HYPERLINK(1), vip"
    assert exports.safe(["normal", "vip"]) == "normal, vip"
    assert exports.safe({"k": "=1"}) == "{'k': '=1'}"
    assert "accounts" in exports.SHEETS
    assert EXPORT_PERMISSIONS["accounts"] == "resellers.manage"


def test_csv_safe_guards_leading_whitespace() -> None:
    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("  =1+1") == "'  =1+1"
    assert _csv_safe("  +2") == "'  +2"
    assert _csv_safe("normal plan") == "normal plan"


def test_business_persian_comma_and_bot_placeholder() -> None:
    rules = business.validate_rules([{"keywords": "price\u060c cost, help", "reply": "Go to {bot}"}])
    assert rules[0]["keywords"] == ["price", "cost", "help"]
    assert business.match(rules, "what is the cost?") == "Go to {bot}"
    src = inspect.getsource(business.reply_for)
    assert 'answer.replace("{bot}", link)' in src


def test_forced_join_resolves_before_locking_shop() -> None:
    src = inspect.getsource(forced_join.add)
    assert src.index("await resolve(") < src.index("await lock_shop(")


def test_announcements_mark_uses_share_lock() -> None:
    from paurubot.core import announcements

    src = inspect.getsource(announcements.mark)
    assert ".with_for_update(read=True)" in src


def test_reports_sold_gb_covers_account_orders() -> None:
    ap = SimpleNamespace(data_gb=500)
    p_order = SimpleNamespace(kind=OrderKind.ACCOUNT_PURCHASE, params={"account_plan_id": 7}, plan=None)
    assert reports._sold_gb(p_order, {7: ap}) == 500
    gb_order = SimpleNamespace(kind=OrderKind.ACCOUNT_GB, params={"gb": 120}, plan=None)
    assert reports._sold_gb(gb_order, {7: ap}) == 120
    renew_pkg = SimpleNamespace(
        kind=OrderKind.ACCOUNT_RENEW,
        params={"account_plan_id": 7, "renew": {"days": 30, "gb": 250}},
        plan=None,
    )
    assert reports._sold_gb(renew_pkg, {7: ap}) == 250
    renew_plain = SimpleNamespace(kind=OrderKind.ACCOUNT_RENEW, params={"account_plan_id": 7}, plan=None)
    assert reports._sold_gb(renew_plain, {7: ap}) == 500


def test_broadcast_handler_guards_stop_and_account_buyers() -> None:
    from paurubot.notifications import handlers

    run_src = inspect.getsource(handlers.run_broadcast)
    assert "TelegramRetryAfter" in run_src
    assert run_src.count("broadcasts.should_stop(row, run)") >= 2
    aud_src = inspect.getsource(handlers.audience_conditions)
    assert 'kind == "account_buyers"' in aud_src
    assert "ResellerProfile.suspended_at.is_(None)" in aud_src
    assert "StockItem" in aud_src
