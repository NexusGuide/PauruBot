from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import catalog, orders, pricing, trials, usernames
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    DiscountCode,
    DiscountKind,
    Job,
    Location,
    OrderKind,
    OrderStatus,
    Panel,
    PlanAudience,
    ResellerGroup,
    ResellerProfile,
    Service,
    ServiceStatus,
    UsernameMethod,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from tests import factories


async def _reseller(session, shop, customer, percent: int = 20) -> None:  # type: ignore[no-untyped-def]
    group = ResellerGroup(shop_id=shop.id, title="Gold", discount_percent=percent)
    session.add(group)
    await session.flush()
    session.add(ResellerProfile(shop_id=shop.id, customer_id=customer.id, group_id=group.id))
    await session.flush()
    await session.refresh(customer, ["reseller_profile"])


def test_quote_rounding_and_caps() -> None:
    code = DiscountCode(kind=DiscountKind.PERCENT, value=15, max_discount=10_000)
    q = pricing.quote(100_000, None, code)
    assert (q.code_discount, q.amount) == (10_000, 90_000)
    fixed = DiscountCode(kind=DiscountKind.FIXED, value=500_000)
    assert pricing.quote(100_000, None, fixed).amount == 0


async def test_reseller_discount_and_audience(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    public = await factories.plan(session, shop, panel, price=100_000)
    reseller_only = await factories.plan(session, shop, panel, price=80_000, title="R")
    reseller_only.audience = PlanAudience.RESELLERS
    buyer = await factories.customer(session, shop)
    agent = await factories.customer(session, shop)
    await _reseller(session, shop, agent, 20)
    assert [p.id for p in await catalog.plans(session, shop.id, buyer)] == [public.id]
    assert {p.id for p in await catalog.plans(session, shop.id, agent)} == {public.id, reseller_only.id}
    assert pricing.quote(public.price, agent).amount == 80_000
    panel.is_active = False
    assert await catalog.plans(session, shop.id, buyer) == []


async def test_discount_rules(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    other = await factories.plan(session, shop, panel, title="Other")
    cust = await factories.customer(session, shop)
    code = DiscountCode(
        shop_id=shop.id,
        code="OFF10",
        value=10,
        max_uses=1,
        per_customer_limit=1,
        plan_ids=[plan.id],
        min_amount=50_000,
    )
    session.add(code)
    await session.flush()
    assert await pricing.find_code(session, shop.id, " off10 ") is code
    await pricing.validate_code(session, code, cust, plan_id=plan.id, amount=100_000)
    with pytest.raises(pricing.DiscountError, match="discount_wrong_plan"):
        await pricing.validate_code(session, code, cust, plan_id=other.id, amount=100_000)
    with pytest.raises(pricing.DiscountError, match="discount_min_amount"):
        await pricing.validate_code(session, code, cust, plan_id=plan.id, amount=10_000)
    code.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(pricing.DiscountError, match="discount_expired"):
        await pricing.validate_code(session, code, cust, plan_id=plan.id, amount=100_000)


async def test_wallet_purchase_enqueues_provisioning(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000)
    cust = await factories.customer(session, shop)
    code = DiscountCode(shop_id=shop.id, code="HALF", value=50, sticky=True)
    session.add(code)
    await session.flush()
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan), code=code
    )
    assert order.amount == 50_000
    with pytest.raises(orders.OrderError, match="insufficient_balance"):
        await orders.settle_from_wallet(session, order.id)
    await wallet.topup(session, shop.id, cust.id, 60_000, provider="card", payment_id=1)
    paid = await orders.settle_from_wallet(session, order.id)
    assert paid.status == OrderStatus.PAID
    assert await wallet_balance(session, shop.id, cust.id) == 10_000
    jobs = (await session.scalars(select(Job).where(Job.dedupe_key == f"order:{order.id}:provision"))).all()
    assert len(jobs) == 1
    assert code.used_count == 1
    assert await pricing.sticky_code(session, cust) is code
    with pytest.raises(orders.OrderError, match="order_expired"):
        await orders.settle_from_wallet(session, order.id)
    await orders.refund_failed(session, paid, "panel down")
    assert await wallet_balance(session, shop.id, cust.id) == 60_000
    assert paid.status == OrderStatus.REFUNDED and code.used_count == 0


async def test_expired_orders_cannot_be_paid(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=0)
    cust = await factories.customer(session, shop)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    order.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.flush()
    with pytest.raises(orders.OrderError):
        await orders.settle_from_wallet(session, order.id)
    assert order.status == OrderStatus.CANCELED


async def test_extras_and_upgrade_pricing(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    small = await factories.plan(session, shop, panel, price=100_000, days=30)
    big = await factories.plan(session, shop, panel, price=250_000, days=30, title="Big")
    cust = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        plan_id=small.id,
        panel_username="u1",
        status=ServiceStatus.ACTIVE,
        expire_at=datetime.now(UTC) + timedelta(days=15),
    )
    session.add(service)
    await session.flush()
    await session.refresh(service, ["plan"])
    vol = await orders.draft(session, cust, OrderKind.EXTRA_VOLUME, service=service, extra_gb=10)
    assert vol.base_price == 10 * 3_000
    up = await orders.draft(session, cust, OrderKind.UPGRADE, service=service, plan=big)
    assert 195_000 <= up.base_price <= 205_000  # about half of the small plan credited
    stranger = await factories.customer(session, shop)
    with pytest.raises(orders.OrderError, match="service_not_found"):
        await orders.draft(session, stranger, OrderKind.EXTRA_DAYS, service=service, extra_days=5)


async def test_trial_policy(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await factories.panel(session, shop)
    cust = await factories.customer(session, shop)
    order = await trials.request(session, cust)
    assert order.kind == OrderKind.TRIAL and order.amount == 0
    with pytest.raises(trials.TrialError, match="trial_already_used"):
        await trials.request(session, cust)
    await shop_settings.set_many(session, shop.id, {"trial_generation": 2})
    assert await trials.eligibility(session, cust) is None
    await shop_settings.set_many(session, shop.id, {"trial_daily_quota": 1})
    other = await factories.customer(session, shop)
    assert await trials.eligibility(session, other) == "trial_quota_reached"
    await shop_settings.set_many(session, shop.id, {"trial_daily_quota": 0, "trial_requires_phone": True})
    assert await trials.eligibility(session, other) == "trial_needs_phone"


async def test_trial_reservation_is_race_free(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Trial race")
        await factories.panel(s, shop)
        cust = await factories.customer(s, shop)
        await s.commit()
        cust_id = cust.id
        shop_id = shop.id

    async def attempt() -> bool:
        async with maker() as s2:
            from paurubot.db.models import Customer

            customer = await s2.get(Customer, cust_id)
            try:
                await trials.request(s2, customer)
                await s2.commit()
                return True
            except trials.TrialError:
                await s2.rollback()
                return False

    results = await asyncio.gather(*(attempt() for _ in range(8)))
    assert sum(results) == 1
    async with maker() as s3:
        from paurubot.db.models import TrialGrant

        assert await s3.scalar(select(func.count(TrialGrant.id)).where(TrialGrant.shop_id == shop_id)) == 1


async def test_username_methods(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    cust = await factories.customer(session, shop, telegram_id=555666, username="Ali-Reza")
    panel.username_prefix = "PB"
    name = await usernames.generate(session, panel, cust)
    assert name.startswith("pb555666_") and len(name) <= 32
    panel.username_method = UsernameMethod.TELEGRAM_USERNAME
    assert (await usernames.generate(session, panel, cust)).startswith("pbali_reza_")
    panel.username_method = UsernameMethod.SEQUENTIAL
    first = await usernames.generate(session, panel, cust)
    second = await usernames.generate(session, panel, cust)
    assert first != second and first.startswith("pb")
    panel.username_method = UsernameMethod.CUSTOM_LABEL
    assert (await usernames.generate(session, panel, cust, "Office ۱ PC")).startswith("pboffice__pc")


async def test_location_pool_picks_least_loaded_panel_with_room(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    loc = Location(shop_id=shop.id, title="DE")
    session.add(loc)
    await session.flush()
    a = await factories.panel(session, shop)
    b = await factories.panel(session, shop)
    for p in (a, b):
        p.location_id = loc.id
        p.capacity = 1
    plan = await factories.plan(session, shop, a)
    plan.panel_id = None
    plan.location_id = loc.id
    cust = await factories.customer(session, shop)
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=a.id,
            panel_username="x1",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.flush()
    chosen = await catalog.pick_panel(session, plan)
    assert chosen is not None and chosen.id == b.id
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=cust.id,
            panel_id=b.id,
            panel_username="x2",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.flush()
    assert await catalog.pick_panel(session, plan) is None
    assert isinstance(a, Panel)
