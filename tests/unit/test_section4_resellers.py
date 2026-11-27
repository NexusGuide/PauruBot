"""Unit tests for Section 4 fixes and features (resellers, bots, PAYG, panel accounts)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from paurubot.core import account_auto_renew, panel_accounts, resale, resellers
from paurubot.db.models import OrderKind, ShopStatus
from paurubot.panels import GB


def test_resale_validate_prices_rejects_bool() -> None:
    with pytest.raises(resale.ResaleError) as exc:
        resale.validate_prices({"1": True})
    assert exc.value.key == "resale_prices_invalid"
    assert resale.validate_prices({"1": 50_000}) == {"1": 50_000}


@pytest.mark.asyncio
async def test_resale_update_rejects_bool_markup() -> None:
    shop = SimpleNamespace(id=10, reseller_profile_id=2)
    with pytest.raises(resale.ResaleError) as exc:
        await resale.update(AsyncMock(), shop, {"resale_markup_percent": True})
    assert exc.value.key == "resale_markup_invalid"


@pytest.mark.asyncio
async def test_resale_disconnect_clears_webhook_before_token() -> None:
    shop = SimpleNamespace(
        id=9,
        status=ShopStatus.ACTIVE,
        bot_token_enc="enc",
        bot_user_id=1234,
    )
    profile = SimpleNamespace(id=3, shop_id=1)
    session = AsyncMock()
    events: list[str] = []

    async def fake_clear(shop_id: int) -> None:
        assert shop.bot_token_enc == "enc"
        events.append(f"clear:{shop_id}")

    with (
        patch.object(resale, "shop_of", AsyncMock(return_value=shop)),
        patch.object(resale, "clear_webhook", side_effect=fake_clear),
        patch(
            "paurubot.notifications.telegram.forget_shop_bot",
            side_effect=lambda sid: events.append(f"forget:{sid}"),
        ),
    ):
        out = await resale.disconnect(session, profile)
    assert out is shop
    assert shop.bot_token_enc is None
    assert shop.status == ShopStatus.SUSPENDED
    assert events == ["clear:9", "forget:9"]


@pytest.mark.asyncio
async def test_webhook_status_reports_health_and_mismatch() -> None:
    info = SimpleNamespace(
        url="https://bot.example.com/api/telegram/webhook/7",
        pending_update_count=2,
        last_error_date=None,
        last_error_message=None,
    )
    bot = AsyncMock()
    bot.get_webhook_info = AsyncMock(return_value=info)
    cfg = SimpleNamespace(bot_mode="webhook", public_url="https://bot.example.com")
    with (
        patch("paurubot.config.get_settings", return_value=cfg),
        patch("paurubot.notifications.telegram.bot_for", AsyncMock(return_value=bot)),
    ):
        status = await resale.webhook_status(7)
    assert status["ok"] is True
    assert status["matches"] is True
    assert status["pending_update_count"] == 2

    info.url = "https://other.example.com/hook"
    with (
        patch("paurubot.config.get_settings", return_value=cfg),
        patch("paurubot.notifications.telegram.bot_for", AsyncMock(return_value=bot)),
    ):
        bad = await resale.webhook_status(7)
    assert bad["ok"] is False
    assert bad["matches"] is False


@pytest.mark.asyncio
async def test_debt_collect_is_deterministic_and_records_snapshot() -> None:
    now = datetime(2026, 11, 26, 10, 30, tzinfo=UTC)
    start = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    profile = SimpleNamespace(id=4, shop_id=1, customer_id=20, billing_debt=50_000)
    session = AsyncMock()
    seen_keys: list[str] = []

    async def fake_collect(_s, prof, amount: int, key: str) -> int:
        seen_keys.append(key)
        prof.billing_debt = 20_000
        return 30_000

    with patch.object(resellers, "_collect", side_effect=fake_collect):
        charged, snap = await resellers._debt_collect(session, profile, now, start)
    assert charged == 30_000
    assert snap is not None
    assert snap.charged == 30_000
    assert snap.debt_after == 20_000
    assert seen_keys == [f"reseller:4:debt:{int(start.timestamp())}:50000"]


@pytest.mark.asyncio
async def test_pay_commission_counts_subreseller_dedicated_bot_sale() -> None:
    parent = SimpleNamespace(id=1, shop_id=1, customer_id=10, is_current=True, discount_percent=30)
    child = SimpleNamespace(
        id=2, shop_id=1, customer_id=20, parent_id=1, is_current=True, discount_percent=10
    )
    parent_customer = SimpleNamespace(id=10, shop_id=1, reseller_profile=parent)
    plan = SimpleNamespace(id=5, price=100_000)
    order = SimpleNamespace(
        id=99,
        shop_id=42,
        customer_id=500,
        plan_id=5,
        kind=OrderKind.PURCHASE,
        base_price=120_000,
        reseller_discount=0,
        amount=0,
        params={"resale_profile": 2, "wholesale": 90_000},
    )
    session = AsyncMock()

    async def fake_get(model, obj_id):
        name = model.__name__
        if name == "ResellerProfile":
            return child if obj_id == 2 else parent
        if name == "Customer":
            return parent_customer
        if name == "Plan":
            return plan
        return None

    session.get = AsyncMock(side_effect=fake_get)
    quote_obj = SimpleNamespace(amount=70_000)
    with (
        patch.object(resellers.pricing, "plan_quote", AsyncMock(return_value=quote_obj)),
        patch.object(resellers.wallet, "commission", AsyncMock()) as comm,
    ):
        paid = await resellers.pay_commission(session, order)
    assert paid == 20_000
    comm.assert_awaited_once()
    assert comm.await_args.args[1] == 1
    assert comm.await_args.args[2] == 10
    assert comm.await_args.args[3] == 20_000


@pytest.mark.asyncio
async def test_pay_commission_gives_no_margin_on_a_subreseller_trial_fee() -> None:
    parent = SimpleNamespace(id=1, shop_id=1, customer_id=10, is_current=True, discount_percent=30)
    child = SimpleNamespace(
        id=2, shop_id=1, customer_id=20, parent_id=1, is_current=True, discount_percent=10
    )
    order = SimpleNamespace(
        id=100,
        shop_id=42,
        customer_id=500,
        plan_id=None,
        kind=OrderKind.TRIAL,
        base_price=0,
        reseller_discount=0,
        amount=0,
        params={"resale_profile": 2, "wholesale": 20_000},
    )
    session = AsyncMock()

    async def fake_get(model, obj_id):
        if model.__name__ == "ResellerProfile":
            return child if obj_id == 2 else parent
        return None

    session.get = AsyncMock(side_effect=fake_get)
    with patch.object(resellers.wallet, "commission", AsyncMock()) as comm:
        paid = await resellers.pay_commission(session, order)
    assert paid == 0
    comm.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_purchase_blocks_upgrade_over_max_total_gb() -> None:
    profile = SimpleNamespace(
        id=8,
        is_current=True,
        suspended_at=None,
        max_purchases_per_day=None,
        max_active_services=None,
        max_total_gb=100,
        group=None,
    )
    customer = SimpleNamespace(id=11, reseller_profile=profile)
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[77, 90 * GB])
    with pytest.raises(resellers.ResellerError) as exc:
        await resellers.check_purchase(
            session,
            customer,
            kind=OrderKind.UPGRADE,
            add_gb=50,
            replace_bytes=20 * GB,
        )
    assert exc.value.key == "reseller_volume_limit"


@pytest.mark.asyncio
async def test_account_auto_renew_skips_banned_and_national_mode() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    account = SimpleNamespace(
        id=5,
        shop_id=1,
        customer_id=33,
        panel_id=2,
        status="active",
        expire_at=now + timedelta(minutes=15),
        data_limit=0,
        used_bytes=0,
        auto_renew={"enabled": True},
        plan=SimpleNamespace(
            is_active=True,
            renewable=True,
            renew_packages_enabled=False,
            renew_custom_enabled=False,
            renew_price=10_000,
            renew_packages=[],
        ),
    )

    def make_session(customer, panel):
        s = AsyncMock()
        s.scalar = AsyncMock(side_effect=[account, None])
        s.get = AsyncMock(side_effect=lambda model, _id: customer if model.__name__ == "Customer" else panel)
        return s

    res_banned = await account_auto_renew.run_account(
        make_session(
            SimpleNamespace(id=33, is_banned=True),
            SimpleNamespace(is_active=True, national_mode=False),
        ),
        5,
        now=now,
    )
    assert res_banned == "banned"

    res_nat = await account_auto_renew.run_account(
        make_session(
            SimpleNamespace(id=33, is_banned=False),
            SimpleNamespace(is_active=True, national_mode=True),
        ),
        5,
        now=now,
    )
    assert res_nat == "national_mode"

    res_off = await account_auto_renew.run_account(
        make_session(
            SimpleNamespace(id=33, is_banned=False),
            SimpleNamespace(is_active=False, national_mode=False),
        ),
        5,
        now=now,
    )
    assert res_off == "panel_inactive"


@pytest.mark.asyncio
async def test_panel_account_remove_clears_auto_renew_and_rechecks_own_bot() -> None:
    account = SimpleNamespace(
        id=14,
        panel_id=2,
        username="adm_test",
        status="active",
        auto_renew={"enabled": True},
    )
    driver = AsyncMock()
    session = AsyncMock()
    recheck = AsyncMock(return_value=[(SimpleNamespace(id=99), "own_paused_inactive")])
    announce = AsyncMock()
    with (
        patch.object(panel_accounts, "_driver", AsyncMock(return_value=driver)),
        patch("paurubot.core.own_panel.recheck_account", recheck),
        patch("paurubot.core.own_panel.announce", announce),
    ):
        await panel_accounts.remove(session, account)
    assert account.status == "deleted"
    assert account.auto_renew is None
    recheck.assert_awaited_once_with(session, account)
    announce.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_logs_merges_filters_and_redacts_for_reseller() -> None:
    from paurubot.db.models import JobStatus, OrderStatus, PaymentPurpose, PaymentStatus

    t1 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 26, 10, 5, tzinfo=UTC)
    t3 = datetime(2026, 11, 26, 10, 10, tzinfo=UTC)
    t4 = datetime(2026, 11, 26, 10, 15, tzinfo=UTC)

    shop = SimpleNamespace(
        id=55,
        parent_id=1,
        title="Ali VPN",
        bot_username="alivpn_bot",
        reseller_profile_id=7,
        own_account_id=None,
    )
    order = SimpleNamespace(
        id=101,
        customer_id=9,
        customer=SimpleNamespace(staff_label="Reza"),
        plan=SimpleNamespace(title="30GB"),
        kind=OrderKind.PURCHASE,
        status=OrderStatus.FAILED,
        amount=120_000,
        params={"wholesale": 90_000},
        error="panel_timeout",
        source="bot",
        created_at=t1,
        updated_at=t2,
    )
    payment = SimpleNamespace(
        id=202,
        order_id=101,
        customer_id=9,
        customer=SimpleNamespace(staff_label="Reza"),
        provider="card",
        purpose=PaymentPurpose.ORDER,
        status=PaymentStatus.PAID,
        amount=120_000,
        review_note="ok",
        failure_reason=None,
        created_at=t1,
        updated_at=t1,
    )
    job = SimpleNamespace(
        id=303,
        queue="provision",
        kind="deliver",
        status=JobStatus.DEAD,
        attempts=8,
        max_attempts=8,
        last_error="502 Bad Gateway",
        payload={"order_id": 101},
        created_at=t3,
        updated_at=t3,
    )
    audit = SimpleNamespace(
        id=404,
        action="reseller_bot.updated",
        actor_type="staff",
        actor_id=1,
        entity="reseller_profile",
        entity_id=7,
        before={"a": 1},
        after={"a": 2},
        ip="1.2.3.4",
        created_at=t4,
    )
    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[[order], [payment], [job], [audit]])

    res = await resale.bot_logs(session, shop, category="errors", staff=True)
    assert res["counts"] == {"all": 4, "errors": 2, "orders": 1, "payments": 1, "jobs": 1, "audit": 1}
    assert [r["id"] for r in res["items"]] == ["job:303", "order:101"]

    csv_bytes = resale.bot_logs_csv(res)
    assert b"502 Bad Gateway" in csv_bytes
    assert b"panel_timeout" in csv_bytes
    assert b"' =HYPERLINK" in resale.bot_logs_csv(
        {
            "items": [
                {
                    "at": "2026-11-26T10:00:00+00:00",
                    "category": "orders",
                    "level": "info",
                    "title": ' =HYPERLINK("https://evil","x")',
                    "status": "completed",
                    "actor": "customer",
                    "detail": "",
                    "amount": 0,
                    "error": None,
                }
            ]
        }
    )

    session.scalars = AsyncMock(side_effect=[[order], [payment], [job], [audit]])
    res_self = await resale.bot_logs(session, shop, category="audit", staff=False)
    assert len(res_self["items"]) == 1
    assert res_self["items"][0]["meta"] is None
    assert res_self["items"][0]["actor"] == "staff"
