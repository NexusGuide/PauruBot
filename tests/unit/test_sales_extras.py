"""Renewal modes (SVC-13), paid reseller membership (TEN-16), referral on every purchase (GRW-09),
customers deleting their own service with a refund (SVC-14) and private discount codes (GRW-10)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from paurubot.core import cancellation, pricing, resellers
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    DiscountCode,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    ResellerGroup,
    Service,
    ServiceStatus,
    Staff,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from paurubot.provisioning.lifecycle import renew_mode, renewal_target
from paurubot.provisioning.rewards import reward_referrer
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


def _service(plan, *, used_gb=4, limit_gb=10, days_left=10, status=ServiceStatus.ACTIVE):  # type: ignore[no-untyped-def]
    return Service(
        id=1,
        plan_id=plan.id,
        panel_username="u",
        status=status,
        data_limit=limit_gb * GB,
        used_bytes=used_gb * GB,
        expire_at=utcnow() + timedelta(days=days_left),
    )


async def test_renewal_modes(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, days=30, data_gb=20)
    service = _service(plan)
    end, limit, reset = renewal_target(service, plan, "extend_reset")
    assert (
        end is not None
        and end - service.expire_at == timedelta(days=30)
        and (limit, reset) == (20 * GB, True)
    )
    end, limit, reset = renewal_target(service, plan, "extend_carry")
    assert (limit, reset) == (4 * GB + 6 * GB + 20 * GB, False)
    end, limit, reset = renewal_target(service, plan, "restart_reset")
    assert end is not None and abs((end - utcnow()).total_seconds() - 30 * 86400) < 5 and reset
    end, limit, reset = renewal_target(service, plan, "restart_carry")
    assert (limit, reset) == (6 * GB + 20 * GB, True)  # the leftover 6 GB carries into a fresh period
    lapsed = _service(plan, status=ServiceStatus.EXPIRED, days_left=-2)
    end, limit, reset = renewal_target(lapsed, plan, "extend_carry")
    assert (limit, reset) == (20 * GB, True)  # nothing carries out of an ended service
    cfg = {"renew_resets_usage": False, "renew_mode": ""}
    assert renew_mode(plan, panel, cfg) == "extend_carry"
    assert renew_mode(plan, panel, {**cfg, "renew_mode": "restart_reset"}) == "restart_reset"
    plan.renew_mode = "restart_carry"
    assert renew_mode(plan, panel, cfg) == "restart_carry"


async def test_paid_membership(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    group = ResellerGroup(shop_id=shop.id, title="Silver", discount_percent=15)
    session.add(group)
    await session.flush()
    cust = await factories.customer(session, shop)
    with pytest.raises(resellers.ResellerError) as off:
        await resellers.buy_membership(session, cust, key="k0")
    assert off.value.key == "membership_off"
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "reseller_membership_price": 500_000,
            "reseller_membership_group_id": group.id,
            "reseller_membership_days": 30,
        },
    )
    with pytest.raises(resellers.ResellerError) as broke:
        await resellers.buy_membership(session, cust, key="k1")
    assert broke.value.key == "insufficient_balance"
    await wallet.topup(session, shop.id, cust.id, 1_200_000, provider="card", payment_id=cust.id)
    profile = await resellers.buy_membership(session, cust, key="k2")
    assert profile.group_id == group.id and profile.is_current and cust.is_reseller
    first_end = profile.expires_at
    assert first_end is not None and first_end > utcnow() + timedelta(days=29)
    again = await resellers.buy_membership(session, cust, key="k2")  # the same button twice
    assert again.expires_at == first_end
    assert await wallet_balance(session, shop.id, cust.id) == 700_000
    await resellers.buy_membership(session, cust, key="k3")  # renews from the current end
    assert profile.expires_at is not None and profile.expires_at - first_end >= timedelta(days=29)
    assert await session.scalar(select(Staff).where(Staff.reseller_profile_id == profile.id)) is not None
    profile.is_active = False  # switched off by staff: never reopened by paying
    with pytest.raises(resellers.ResellerError):
        await resellers.buy_membership(session, cust, key="k4")


async def _paid(session, shop, customer, amount):  # type: ignore[no-untyped-def]
    order = Order(
        shop_id=shop.id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        base_price=amount,
        amount=amount,
        params={},
    )
    session.add(order)
    await session.flush()
    return order


async def test_referral_on_every_purchase_with_monthly_cap(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "referral_percent": 10,
            "referral_every_purchase": True,
            "referral_repeat_percent": 5,
            "referral_monthly_max": 3,
        },
    )
    inviter = await factories.customer(session, shop)
    friend = await factories.customer(session, shop)
    friend.referrer_id = inviter.id
    await session.flush()
    rewards = [await reward_referrer(session, await _paid(session, shop, friend, 100_000)) for _ in range(4)]
    assert rewards == [10_000, 5_000, 5_000, 0]  # first at 10%, then 5%, capped at 3 a month
    await shop_settings.set_many(
        session, shop.id, {"referral_every_purchase": False, "referral_monthly_max": 0}
    )
    assert await reward_referrer(session, await _paid(session, shop, friend, 100_000)) == 0


async def test_customer_deletes_service_with_refund_once(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    try:
        plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=10)
        cust = await factories.customer(session, shop)
        service = Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=panel.id,
            plan_id=plan.id,
            panel_username="u_cancel",
            status=ServiceStatus.ACTIVE,
            data_limit=10 * GB,
            used_bytes=2 * GB,  # 80% of the data left
            expire_at=utcnow() + timedelta(days=15),  # half the period left
        )
        session.add(service)
        await session.flush()
        fake.users["u_cancel"] = {
            "username": "u_cancel",
            "status": "active",
            "used_traffic": 0,
            "data_limit": 0,
            "expire": 0,
            "sub": "s",
        }
        paid = await _paid(session, shop, cust, 100_000)
        paid.service_id, paid.completed_at = service.id, utcnow()
        await session.refresh(service, ["plan"])
        await shop_settings.set_many(session, shop.id, {"self_delete_refund_percent": 80})
        with pytest.raises(cancellation.CancelError):
            await cancellation.cancel(session, cust, service)  # switched off by default
        await shop_settings.set_many(session, shop.id, {"self_delete_enabled": True})
        assert await cancellation.quote(session, cust, service) == 40_000  # 100k x 50% left x 80%
        assert await cancellation.cancel(session, cust, service) == 40_000
        assert service.status == ServiceStatus.DELETED and "u_cancel" not in fake.users
        assert await wallet_balance(session, shop.id, cust.id) == 40_000
        with pytest.raises(cancellation.CancelError):
            await cancellation.cancel(session, cust, service)
    finally:
        registry.test_transports.clear()
        registry._cache.clear()


async def test_private_discount_codes(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    owner = await factories.customer(session, shop)
    other = await factories.customer(session, shop)
    code = DiscountCode(shop_id=shop.id, code="ALI10", value=10, customer_id=owner.id)
    session.add(code)
    await session.flush()
    await pricing.validate_code(session, code, owner, plan_id=None, amount=100_000)
    with pytest.raises(pricing.DiscountError):
        await pricing.validate_code(session, code, other, plan_id=None, amount=100_000)
    assert [c.code for c in await pricing.private_codes(session, owner)] == ["ALI10"]
    assert await pricing.private_codes(session, other) == []
