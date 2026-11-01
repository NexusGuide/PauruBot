"""SEC-F01: an upgrade credits what was really paid for the unused part of the period.

credit = amount paid for the current period × min(time left, data left); upgrades go only to a
more expensive plan. The old rule credited the current list price for the time left, which let a
customer use up the data, "upgrade" to the same plan and get fresh data for a few percent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from paurubot.core import orders
from paurubot.db.models import Order, OrderKind, OrderStatus, Service, ServiceStatus
from paurubot.panels import GB
from tests import factories


async def _env(session, *, used_gb: int = 0, days_left: float = 30):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    small = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=100)
    big = await factories.plan(session, shop, panel, price=250_000, days=30, data_gb=300, title="Big")
    cheap = await factories.plan(session, shop, panel, price=60_000, days=30, data_gb=50, title="Cheap")
    cust = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        plan_id=small.id,
        panel_username=f"u{cust.id}",
        status=ServiceStatus.ACTIVE,
        data_limit=100 * GB,
        used_bytes=used_gb * GB,
        expire_at=datetime.now(UTC) + timedelta(days=days_left),
    )
    session.add(service)
    await session.flush()
    await session.refresh(service, ["plan", "panel"])
    return shop, small, big, cheap, cust, service


def _paid(shop, cust, service, plan, amount: int, kind=OrderKind.PURCHASE, **params):  # type: ignore[no-untyped-def]
    return Order(
        shop_id=shop.id,
        customer_id=cust.id,
        kind=kind,
        status=OrderStatus.COMPLETED,
        plan_id=plan.id,
        service_id=service.id,
        base_price=amount,
        amount=amount,
        params=params,
        completed_at=datetime.now(UTC),
    )


async def test_used_up_data_is_not_credited(session) -> None:  # type: ignore[no-untyped-def]
    shop, small, big, _, cust, service = await _env(session, used_gb=95, days_left=29.9)
    session.add(_paid(shop, cust, service, small, 100_000))
    await session.flush()
    up = await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=big)
    # Almost all time left, 5% of the data left: the smaller share counts.
    assert 4_900 <= up.params["credit"] <= 5_000
    assert up.base_price == 250_000 - up.params["credit"]


async def test_credit_is_what_was_paid_not_the_list_price(session) -> None:  # type: ignore[no-untyped-def]
    shop, small, big, _, cust, service = await _env(session, days_left=15)
    session.add(_paid(shop, cust, service, small, 40_000))  # bought with a 60% code
    await session.flush()
    small.price = 180_000  # and the list price went up since
    up = await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=big)
    assert 19_900 <= up.params["credit"] <= 20_000  # half of the 40,000 actually paid


async def test_upgrade_needs_a_more_expensive_plan(session) -> None:  # type: ignore[no-untyped-def]
    shop, small, _, cheap, cust, service = await _env(session, used_gb=99, days_left=29)
    session.add(_paid(shop, cust, service, small, 100_000))
    await session.flush()
    with pytest.raises(orders.OrderError, match="upgrade_not_higher"):
        await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=small)
    with pytest.raises(orders.OrderError, match="upgrade_not_higher"):
        await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=cheap)
    same_price = await factories.plan(
        session, shop, small.panel, price=100_000, days=60, data_gb=100, title="Same price"
    )
    with pytest.raises(orders.OrderError, match="upgrade_not_higher"):
        await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=same_price)


async def test_bot_lists_only_plans_an_upgrade_may_target(session) -> None:  # type: ignore[no-untyped-def]
    _, small, big, cheap, _, service = await _env(session)
    floor = await orders.upgrade_floor(session, service)
    assert floor == 100_000
    assert [p.title for p in (small, big, cheap) if orders.upgradable(p, service, floor)] == ["Big"]


async def test_an_upgrade_period_is_worth_its_payment_plus_its_credit(session) -> None:  # type: ignore[no-untyped-def]
    shop, small, big, _, cust, service = await _env(session, days_left=30)
    session.add(_paid(shop, cust, service, big, 150_000, kind=OrderKind.UPGRADE, credit=100_000))
    service.plan_id = big.id
    await session.flush()
    await session.refresh(service, ["plan", "panel"])
    bigger = await factories.plan(session, shop, small.panel, price=400_000, days=30, data_gb=500, title="XL")
    basis = await orders.period_basis(session, service)
    assert basis.paid == 250_000 and basis.days == 30
    up = await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=bigger)
    assert 249_000 <= up.params["credit"] <= 250_000


async def test_free_periods_and_trials_carry_no_credit(session) -> None:  # type: ignore[no-untyped-def]
    shop, small, big, _, cust, service = await _env(session)
    session.add(_paid(shop, cust, service, small, 0))  # gifted by staff or a 100% code
    await session.flush()
    assert await orders.upgrade_credit(session, service) == 0
    service.is_trial = True
    assert await orders.upgrade_credit(session, service) == 0
