from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from paurubot.core import orders, resellers
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelKind,
    ResellerBillingSnapshot,
    ResellerProfile,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


async def drain(session, name: str) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, name, limit=1)
        if not claimed:
            return total
        await run_job(name, QUEUES[name], claimed[0])
        total += 1


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
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _reseller(session, shop, *, balance: int = 1_000_000, **fields):  # type: ignore[no-untyped-def]
    cust = await factories.customer(session, shop)
    if balance:
        await wallet.topup(session, shop.id, cust.id, balance, provider="card", payment_id=cust.id)
    profile = ResellerProfile(shop_id=shop.id, customer_id=cust.id, permissions=[], **fields)
    session.add(profile)
    await session.flush()
    await session.refresh(cust, ["reseller_profile"])
    await session.commit()
    return cust, profile


async def _buy(session, cust, plan):  # type: ignore[no-untyped-def]
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED, order.error
    return order


async def test_limits_and_expiry(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    cust, profile = await _reseller(session, shop, discount_override=10, max_purchases_per_day=1)
    first = await _buy(session, cust, plan)
    assert first.amount == 90_000
    with pytest.raises(orders.OrderError) as daily:
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert daily.value.key == "reseller_daily_limit" and daily.value.params["limit"] == 1

    profile.max_purchases_per_day = None
    profile.max_active_services = 1
    with pytest.raises(orders.OrderError) as services:
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert services.value.key == "reseller_service_limit"

    profile.max_active_services = None
    profile.max_total_gb = 80
    with pytest.raises(orders.OrderError) as volume:
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert volume.value.key == "reseller_volume_limit"
    service = await session.get(Service, first.service_id)
    assert await orders.draft(session, cust, OrderKind.EXTRA_VOLUME, service=service, extra_gb=30)
    with pytest.raises(orders.OrderError):
        await orders.draft(session, cust, OrderKind.EXTRA_VOLUME, service=service, extra_gb=31)

    profile.max_total_gb = None
    profile.suspended_at = utcnow()
    with pytest.raises(orders.OrderError) as suspended:
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert suspended.value.key == "reseller_suspended_error"

    profile.suspended_at = None
    profile.expires_at = utcnow() - timedelta(minutes=1)
    assert not cust.is_reseller
    draft = await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    assert (await orders.create(session, cust, draft)).amount == 100_000  # back to the normal price


async def test_usage_billing_debt_suspend_and_resume(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, fake = env["shop"], env["plan"], env["fake"]
    cust, profile = await _reseller(session, shop, balance=3_000, billing_mode="usage", gb_price=1_000)
    order = await _buy(session, cust, plan)
    assert order.amount == 0 and order.params["metered"] is True
    service = await session.get(Service, order.service_id)
    start = utcnow() - timedelta(hours=2)
    profile.last_billed_at = start
    service.used_bytes = int(2.5 * GB)
    await session.commit()

    result = await resellers.bill(session, profile, now=start + timedelta(hours=1, minutes=5))
    await session.commit()
    assert result.snapshot is not None and result.snapshot.amount == 2_500
    assert await wallet_balance(session, shop.id, cust.id) == 500
    assert profile.last_billed_at == start + timedelta(hours=1)
    # The same period is never billed twice.
    again = await resellers.bill(session, profile, now=start + timedelta(hours=1, minutes=30))
    assert again.snapshot is None

    service.used_bytes = int(4.5 * GB)  # 2 GB more costs 2,000; only 500 is left
    await session.commit()
    result = await resellers.bill(session, profile, now=start + timedelta(hours=2, minutes=1))
    await session.commit()
    assert result.suspended and profile.billing_debt == 1_500
    assert await wallet_balance(session, shop.id, cust.id) == 0
    await drain(session, "bulk")
    await session.refresh(service)
    assert service.status == ServiceStatus.DISABLED and service.disabled_reason == "reseller"
    assert fake.users[service.panel_username]["status"] == "disabled"
    with pytest.raises(orders.OrderError):
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)

    await wallet.topup(session, shop.id, cust.id, 10_000, provider="card", payment_id=987_654)
    await session.commit()
    result = await resellers.bill(session, profile, now=start + timedelta(hours=2, minutes=20))
    await session.commit()
    assert result.resumed and profile.billing_debt == 0 and profile.suspended_at is None
    assert await wallet_balance(session, shop.id, cust.id) == 8_500
    await drain(session, "bulk")
    await session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE and service.disabled_reason is None
    snapshots = list(
        await session.scalars(
            select(ResellerBillingSnapshot).where(ResellerBillingSnapshot.profile_id == profile.id)
        )
    )
    assert [s.amount for s in snapshots] == [2_500, 2_000]


async def test_hourly_billing_counts_active_service_hours(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    cust, profile = await _reseller(session, shop, billing_mode="hourly", hourly_price=50)
    await _buy(session, cust, plan)
    await _buy(session, cust, plan)
    start = utcnow() - timedelta(hours=5)
    profile.last_billed_at = start
    await session.commit()
    result = await resellers.bill(session, profile, now=start + timedelta(hours=3, minutes=10))
    assert result.snapshot is not None
    assert result.snapshot.service_hours == 6 and result.snapshot.amount == 300


async def test_level_two_resellers_and_commission(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan = env["shop"], env["plan"]
    parent_cust, parent = await _reseller(session, shop, balance=0, discount_override=20)
    buyer = await factories.customer(session, shop)
    with pytest.raises(resellers.ResellerError) as high:
        await resellers.make_child(session, parent, buyer, 25)
    assert high.value.key == "subreseller_discount_too_high"
    child = await resellers.make_child(session, parent, buyer, 10)
    await session.commit()
    assert resellers.level(child) == 2 and buyer.is_reseller
    other = await factories.customer(session, shop)
    with pytest.raises(resellers.ResellerError):
        await resellers.make_child(session, child, other, 5)  # no third level
    assert set(await resellers.scope_customer_ids(session, parent)) == {parent_cust.id, buyer.id}

    await wallet.topup(session, shop.id, buyer.id, 500_000, provider="card", payment_id=buyer.id + 1)
    await session.commit()
    order = await _buy(session, buyer, plan)
    assert order.amount == 90_000
    # The parent would have paid 80,000; the 10,000 margin is its commission.
    assert await wallet_balance(session, shop.id, parent_cust.id) == 10_000


async def test_low_balance_warning_once_a_day(session, env) -> None:  # type: ignore[no-untyped-def]
    _, profile = await _reseller(session, env["shop"], balance=5_000, low_balance_threshold=10_000)
    assert await resellers.warn_low_balance(session, profile)
    assert not await resellers.warn_low_balance(session, profile)
    jobs = list(
        await session.scalars(
            select(Job).where(Job.kind == "reseller_notice", Job.status == JobStatus.QUEUED)
        )
    )
    assert len(jobs) == 1 and jobs[0].payload["notice"] == "low_balance"
