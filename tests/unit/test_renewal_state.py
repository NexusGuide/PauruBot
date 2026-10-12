"""Renewal reservation state machine: no double work, no refund for a change the panel may hold."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.db.models import Job, JobStatus, Order, OrderKind, OrderStatus, PanelKind, Service
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from paurubot.provisioning import lifecycle
from paurubot.provisioning import renewal_state as rs
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


def test_only_allowed_transitions() -> None:
    order = Order(kind=OrderKind.RENEW, params={})
    assert rs.state(order) is None and rs.refundable(order)
    rs.move(order, rs.PENDING)
    assert rs.refundable(order)
    rs.move(order, rs.RUNNING)
    assert not rs.refundable(order)
    with pytest.raises(rs.IllegalTransition):
        rs.move(order, rs.FAILED)  # a running change may have reached the panel
    rs.move(order, rs.UNCERTAIN, "timeout")
    rs.park(order, "job dead")
    assert rs.parked(order) and not rs.refundable(order)
    rs.move(order, rs.RECONCILED)
    assert rs.is_settled(order) and not rs.parked(order)
    with pytest.raises(rs.IllegalTransition):
        rs.move(order, rs.RUNNING)  # settled: never applied again
    data = rs.record(order)
    assert [h["state"] for h in data["history"]] == ["pending", "running", "uncertain", "reconciled"]
    assert data["attempts"] == 1
    # Purchases are outside the machine and keep their refunds.
    assert rs.refundable(Order(kind=OrderKind.PURCHASE, params={}))


async def _drain(session) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, "provision", limit=1)
        if not claimed:
            return total
        await run_job("provision", QUEUES["provision"], claimed[0])
        total += 1


async def _run_until_dead(session, order_id: int) -> None:  # type: ignore[no-untyped-def]
    for _ in range(40):
        job = await session.scalar(
            select(Job)
            .where(Job.dedupe_key == f"order:{order_id}:provision")
            .execution_options(populate_existing=True)
        )
        if job.status in {JobStatus.DEAD, JobStatus.DONE}:
            return
        job.run_at = datetime.now(UTC)
        await session.commit()
        await _drain(session)
    raise AssertionError("job never finished")


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 2_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    yield {"shop": shop, "panel": panel, "plan": plan, "cust": cust, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _buy(session, cust, kind, **kw):  # type: ignore[no-untyped-def]
    from paurubot.core import orders

    order = await orders.create(session, cust, await orders.draft(session, cust, kind, **kw))
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


async def _service(session, env) -> Service:  # type: ignore[no-untyped-def]
    order = await _buy(session, env["cust"], OrderKind.PURCHASE, plan=env["plan"])
    await _drain(session)
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert service is not None
    return service


def _put_hook(env, name: str, *, apply: bool, status: int):  # type: ignore[no-untyped-def]
    """Make the panel answer the user update with an error, after applying it or not."""
    fake, calls = env["fake"], {"put": 0}
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path == f"/api/user/{name}":
            calls["put"] += 1
            if apply:
                original(request)
            return httpx.Response(status, json={"detail": "boom"})
        return original(request)

    registry.test_transports[env["panel"].id] = httpx.MockTransport(handler)
    registry._cache.clear()
    return calls


async def test_lost_response_is_reconciled_without_applying_twice(session, env) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    calls = _put_hook(env, service.panel_username, apply=True, status=502)
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    await _drain(session)
    await session.refresh(renewal)
    assert rs.state(renewal) == rs.UNCERTAIN and renewal.status == OrderStatus.PROVISIONING

    await _run_until_dead(session, renewal.id)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED
    assert rs.state(renewal) == rs.RECONCILED
    assert calls["put"] == 1  # the retry saw the panel already had it


async def test_rejected_renewal_is_parked_then_refunded_by_reconciliation(session, env) -> None:  # type: ignore[no-untyped-def]
    env["plan"].renew_mode = "extend_carry"  # no usage reset: the panel stays exactly as before
    await session.commit()
    service = await _service(session, env)
    _put_hook(env, service.panel_username, apply=False, status=500)
    before = await wallet_balance(session, env["shop"].id, env["cust"].id)
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)
    assert charged < before

    await _run_until_dead(session, renewal.id)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.PROVISIONING  # not refunded blindly
    assert rs.parked(renewal) and rs.state(renewal) == rs.UNCERTAIN
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged
    assert await lifecycle.parked_renewals(session) == [renewal.id]

    assert await lifecycle.reconcile_renewal(session, renewal.id) == "failed"
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.REFUNDED and rs.state(renewal) == rs.FAILED
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == before


async def test_partial_change_stays_uncertain_and_found_applied_later(session, env) -> None:  # type: ignore[no-untyped-def]
    service = await _service(session, env)
    name = service.panel_username
    env["fake"].users[name]["used_traffic"] = 5 * GB
    _put_hook(env, name, apply=False, status=500)
    renewal = await _buy(session, env["cust"], OrderKind.RENEW, plan=env["plan"], service=service)
    charged = await wallet_balance(session, env["shop"].id, env["cust"].id)
    await _run_until_dead(session, renewal.id)
    await session.refresh(renewal)
    # The usage reset reached the panel but the new period did not: neither before nor after.
    assert await lifecycle.reconcile_renewal(session, renewal.id) == "uncertain"
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.PROVISIONING and rs.record(renewal)["checks"] == 1
    assert await wallet_balance(session, env["shop"].id, env["cust"].id) == charged

    # Staff fix the account on the panel by hand: the next look finishes the order.
    target = renewal.params["target"]
    user = env["fake"].users[name]
    user["expire"] = int(datetime.fromisoformat(target["expire"]).timestamp())
    user["data_limit"] = target["limit"]
    assert await lifecycle.reconcile_renewal(session, renewal.id) == "reconciled"
    await _drain(session)
    await session.refresh(renewal)
    assert renewal.status == OrderStatus.COMPLETED and rs.state(renewal) == rs.RECONCILED
