"""Fixed reseller prices, panels hidden from reseller groups and the reseller's panel in the bot
(TEN-12, TEN-13)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update

from paurubot.core import catalog, orders, pricing, reseller_desk, resellers
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelKind,
    ResellerGroup,
    ResellerPrice,
    ResellerProfile,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_resellers import drain


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    group = ResellerGroup(shop_id=shop.id, title="Gold", discount_percent=10)
    session.add(group)
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake, "group": group}
    registry.test_transports.clear()
    registry._cache.clear()


async def _reseller(session, shop, group, *, balance=1_000_000, **fields):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, shop)
    if balance:
        await wallet.topup(session, shop.id, cust.id, balance, provider="card", payment_id=cust.id)
    profile = ResellerProfile(
        shop_id=shop.id, customer_id=cust.id, group_id=group.id, permissions=["resellers.sub"], **fields
    )
    session.add(profile)
    await session.flush()
    await session.refresh(cust, ["reseller_profile"])
    await session.refresh(profile, ["group"])
    await session.commit()
    return cust, profile


async def test_fixed_prices_beat_the_percentage_and_own_beats_group(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, group = env["shop"], env["plan"], env["group"]
    cust, profile = await _reseller(session, shop, group)
    normal = await factories.customer(session, shop)
    assert (await pricing.plan_quote(session, cust, plan.id, plan.price)).amount == 90_000  # 10% group
    session.add(ResellerPrice(shop_id=shop.id, plan_id=plan.id, group_id=group.id, price=70_000))
    await session.flush()
    assert (await pricing.plan_quote(session, cust, plan.id, plan.price)).amount == 70_000
    assert (await pricing.plan_quote(session, normal, plan.id, plan.price)).amount == 100_000
    session.add(ResellerPrice(shop_id=shop.id, plan_id=plan.id, profile_id=profile.id, price=65_000))
    await session.flush()
    q = await pricing.plan_quote(session, cust, plan.id, plan.price)
    assert (q.amount, q.reseller_discount, q.base_price) == (65_000, 35_000, 100_000)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    assert order.amount == 65_000 and order.reseller_discount == 35_000


async def test_level_two_never_pays_less_than_level_one_and_commission_follows(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, group = env["shop"], env["plan"], env["group"]
    parent_cust, parent = await _reseller(session, shop, group)
    session.add(ResellerPrice(shop_id=shop.id, plan_id=plan.id, group_id=group.id, price=60_000))
    target = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, target.id, 500_000, provider="card", payment_id=target.id)
    await session.refresh(parent, ["group"])
    child = await resellers.make_child(session, parent, target, 5)  # 95_000 by percent
    await session.commit()
    quote = await pricing.plan_quote(session, target, plan.id, plan.price)
    assert quote.amount == 95_000  # above the level-1 price: its own percentage stands
    child.discount_override = 10
    await session.flush()
    order = await orders.create(
        session, target, await orders.draft(session, target, OrderKind.PURCHASE, plan=plan)
    )
    assert order.amount == 90_000
    before = await wallet_balance(session, shop.id, parent_cust.id)
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    # The level-1 margin comes from its fixed price (60_000), not its 10% group discount (90_000).
    assert await wallet_balance(session, shop.id, parent_cust.id) - before == 30_000
    child.discount_override = 50  # below the level-1 price: floored there, never a negative margin
    await session.flush()
    assert (await pricing.plan_quote(session, target, plan.id, plan.price)).amount == 60_000


async def test_hidden_panels_disappear_for_that_group_only(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, group, panel = env["shop"], env["plan"], env["group"], env["panel"]
    cust, _profile = await _reseller(session, shop, group)
    normal = await factories.customer(session, shop)
    group.hidden_panel_ids = [panel.id]
    await session.flush()
    assert await catalog.get_plan(session, shop.id, plan.id, cust) is None
    assert plan.id not in [p.id for p in await catalog.plans(session, shop.id, cust)]
    assert await catalog.get_plan(session, shop.id, plan.id, normal) is not None


async def _service(session, shop, panel, plan, cust, label, days, used=0):  # type: ignore[no-untyped-def]
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username=f"u_{label}",
        label=label,
        status=ServiceStatus.ACTIVE,
        data_limit=50 * GB,
        used_bytes=used,
        expire_at=utcnow() + timedelta(days=days),
    )
    session.add(service)
    await session.flush()
    return service


async def test_desk_search_expiring_and_bulk_renewal(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, group, panel, fake = env["shop"], env["plan"], env["group"], env["panel"], env["fake"]
    cust, _profile = await _reseller(session, shop, group, balance=200_000)
    soon_a = await _service(session, shop, panel, plan, cust, "ali_phone", 1)
    soon_b = await _service(session, shop, panel, plan, cust, "reza", 2)
    full = await _service(session, shop, panel, plan, cust, "sara", 20, used=46 * GB)
    later = await _service(session, shop, panel, plan, cust, "mina", 25)
    for s in (soon_a, soon_b, full, later):
        fake.users[s.panel_username] = {
            "id": s.id,
            "username": s.panel_username,
            "status": "active",
            "used_traffic": s.used_bytes,
            "data_limit": s.data_limit,
            "expire": int(s.expire_at.timestamp()),
            "sub": s.panel_username,
        }
    await session.commit()
    assert [s.id for s in await reseller_desk.search(session, cust, "ali")] == [soon_a.id]
    assert [s.id for s in await reseller_desk.search(session, cust, "u_REZA")] == [soon_b.id]
    expiring = await reseller_desk.expiring(session, cust, 3)
    assert {s.id for s in expiring} == {soon_a.id, soon_b.id, full.id}
    planned = await reseller_desk.renew_plan(session, cust, expiring)
    assert planned.total == 3 * 90_000
    # 200_000 covers two renewals at 90_000: the third stops on the balance, the others stand.
    result = await reseller_desk.renew_many(session, cust, expiring)
    await session.commit()
    assert len(result.renewed) == 2
    assert [key for _, key in result.failed] == ["insufficient_balance"]
    assert await wallet_balance(session, shop.id, cust.id) == 20_000
    figures = await reseller_desk.figures(session, cust)
    assert figures.active == 4


async def test_desk_report(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, group = env["shop"], env["plan"], env["group"]
    cust, _profile = await _reseller(session, shop, group)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    data = await reseller_desk.report(session, cust)
    assert data.periods[0] == ("today", 1, 90_000)
    assert data.gb_sold == 50 and data.top_plans[0][1] == 1
