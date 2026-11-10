"""Shop-wide purchase limits (settings › limits): live services per customer, purchases a day."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import OrderKind, OrderStatus, ResellerProfile, Service, ServiceStatus
from tests import factories


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    customer = await factories.customer(session, shop)
    await session.commit()
    return {"shop": shop, "panel": panel, "plan": plan, "customer": customer}


async def _buy(session, env):  # type: ignore[no-untyped-def]
    draft = await orders.draft(session, env["customer"], OrderKind.PURCHASE, plan=env["plan"])
    return await orders.create(session, env["customer"], draft)


async def test_no_limits_by_default(session, env) -> None:  # type: ignore[no-untyped-def]
    for _ in range(3):
        await _buy(session, env)


async def test_live_services_cap_counts_services_and_orders_in_flight(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"max_active_services": 2})
    session.add(
        Service(
            shop_id=env["shop"].id,
            customer_id=env["customer"].id,
            panel_id=env["panel"].id,
            panel_username="lim_1",
            status=ServiceStatus.ACTIVE,
        )
    )
    session.add(
        Service(
            shop_id=env["shop"].id,
            customer_id=env["customer"].id,
            panel_id=env["panel"].id,
            panel_username="lim_old",
            status=ServiceStatus.EXPIRED,  # an ended service does not count
        )
    )
    await session.flush()
    first = await _buy(session, env)
    await _buy(session, env)  # an opened checkout alone holds no place: «buy» twice is fine
    first.status = OrderStatus.AWAITING_PAYMENT  # a receipt sent: 1 live + 1 being paid = 2
    await session.flush()
    with pytest.raises(orders.OrderError) as caught:
        await _buy(session, env)
    assert caught.value.key == "limit_active_services" and caught.value.params == {"limit": 2}
    first.expires_at = utcnow() - timedelta(minutes=1)  # an abandoned payment frees its place
    await session.flush()
    await _buy(session, env)
    first.status = OrderStatus.CANCELED
    await session.flush()
    await _buy(session, env)


async def test_daily_purchases_cap_counts_the_last_day(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"max_daily_purchases": 2})
    a = await _buy(session, env)
    b = await _buy(session, env)
    a.status, b.status = OrderStatus.COMPLETED, OrderStatus.COMPLETED
    await session.flush()
    with pytest.raises(orders.OrderError) as caught:
        await _buy(session, env)
    assert caught.value.key == "limit_daily_purchases"
    a.created_at = utcnow() - timedelta(hours=25)  # bought yesterday
    await session.flush()
    await _buy(session, env)


async def test_resellers_follow_their_own_caps(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, env["shop"].id, {"max_active_services": 1, "max_daily_purchases": 1}
    )
    profile = ResellerProfile(shop_id=env["shop"].id, customer_id=env["customer"].id)
    session.add(profile)
    await session.flush()
    await session.refresh(env["customer"], ["reseller_profile"])
    for _ in range(3):
        await _buy(session, env)


async def test_report_sections_are_checked(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"daily_report_sections": ["money"]})
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, env["shop"].id, {"daily_report_sections": ["money", "gossip"]})


async def test_terms_before_purchase_stop_a_new_order_until_accepted(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, env["shop"].id, {"terms_required": True, "terms_version": 3, "terms_when": "purchase"}
    )
    with pytest.raises(orders.OrderError) as caught:
        await _buy(session, env)
    assert caught.value.key == "terms_required"
    # A guest's web checkout never showed the terms: it is not asked there.
    draft = await orders.draft(session, env["customer"], OrderKind.PURCHASE, plan=env["plan"])
    await orders.create(session, env["customer"], draft, source="web")
    env["customer"].terms_accepted_version = 3
    await _buy(session, env)
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, env["shop"].id, {"terms_when": "sometimes"})
