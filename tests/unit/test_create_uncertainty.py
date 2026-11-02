"""SEC-F08: a purchase whose panel account may exist is never refunded blindly.

The panel can create the account and still fail the request (lost response, timeout, crash, or
the panel going down right after). When the job runs out of retries, the order is parked and
settled by reading the panel: an existing account is adopted, and only a proven-missing one is
refunded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from paurubot.core import orders, trials
from paurubot.db.models import Job, JobStatus, Order, OrderKind, OrderStatus, PanelKind, Service, TrialGrant
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import registry
from paurubot.provisioning import create_state, lifecycle
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from paurubot.worker import queue
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport


async def drain(session) -> int:  # type: ignore[no-untyped-def]
    total = 0
    while True:
        claimed = await queue.claim(session, "provision", limit=1)
        if not claimed:
            return total
        await run_job("provision", QUEUES["provision"], claimed[0])
        total += 1


async def _isolate(session) -> None:  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))


async def _setup(session):  # type: ignore[no-untyped-def]
    await _isolate(session)
    registry._cache.clear()
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    return shop, panel, plan, cust, fake


def _use(panel, handler) -> None:  # type: ignore[no-untyped-def]
    registry.test_transports[panel.id] = httpx.MockTransport(handler)
    registry._cache.clear()


def _down(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="maintenance")


def _create_then_lose(fake, created: bool):  # type: ignore[no-untyped-def]
    """The create request times out; the panel did (or did not) create the account first."""
    original = fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/user":
            if created:
                original(request)
            raise httpx.ReadTimeout("lost response", request=request)
        return original(request)

    return handler


async def _exhaust(session, order_id: int) -> None:  # type: ignore[no-untyped-def]
    """Run the provisioning job until it is dead and its dead hook has run."""
    for _ in range(12):
        job = await session.scalar(
            select(Job)
            .where(Job.dedupe_key == f"order:{order_id}:provision")
            .execution_options(populate_existing=True)
        )
        if job.status == JobStatus.DEAD:
            break
        job.run_at = datetime.now(UTC)
        await session.commit()
        await drain(session)
    await drain(session)  # the dead hook
    job = await session.scalar(
        select(Job)
        .where(Job.dedupe_key == f"order:{order_id}:provision")
        .execution_options(populate_existing=True)
    )
    assert job.status == JobStatus.DEAD


async def _buy(session, cust, plan):  # type: ignore[no-untyped-def]
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


async def _settled(session, order) -> None:  # type: ignore[no-untyped-def]
    """Pretend the settle delay has passed since the last create request."""
    data = create_state.record(order)
    data["attempted_at"] = (datetime.now(UTC) - create_state.SETTLE_DELAY - timedelta(seconds=1)).isoformat()
    order.params = {**order.params, "create": data}
    await session.commit()


async def _first_attempt(session, panel, fake, order, *, created: bool) -> None:  # type: ignore[no-untyped-def]
    _use(panel, _create_then_lose(fake, created))
    claimed = await queue.claim(session, "provision", limit=1)
    await run_job("provision", QUEUES["provision"], claimed[0])
    await session.refresh(order)
    assert create_state.state(order) == create_state.ATTEMPTED
    assert order.status == OrderStatus.PROVISIONING


async def test_account_created_before_the_panel_went_down_is_adopted_not_refunded(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    after_purchase = None
    order = await _buy(session, cust, plan)
    after_purchase = await wallet_balance(session, shop.id, cust.id)
    await _first_attempt(session, panel, fake, order, created=True)
    assert len(fake.users) == 1
    _use(panel, _down)  # every retry fails until the job gives up
    await _exhaust(session, order.id)

    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING  # parked, not refunded
    assert create_state.parked(order)
    assert await wallet_balance(session, shop.id, cust.id) == after_purchase

    # Too early: the reconciler waits for the last request to settle on the panel.
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    assert await lifecycle.reconcile_purchase(session, order.id) == "waiting"

    await _settled(session, order)
    assert await lifecycle.reconcile_purchase(session, order.id) == "resumed"
    await drain(session)
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED and order.service_id
    assert create_state.state(order) == create_state.CONFIRMED
    assert len(fake.users) == 1  # adopted, never created twice
    assert await wallet_balance(session, shop.id, cust.id) == after_purchase


async def test_account_proven_missing_is_refunded(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    before = await wallet_balance(session, shop.id, cust.id)
    order = await _buy(session, cust, plan)
    await _first_attempt(session, panel, fake, order, created=False)
    _use(panel, _down)
    await _exhaust(session, order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING and create_state.parked(order)

    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    await _settled(session, order)
    assert await lifecycle.reconcile_purchase(session, order.id) == "refunded"
    await session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
    assert create_state.state(order) == create_state.ABSENT
    assert await wallet_balance(session, shop.id, cust.id) == before
    assert fake.users == {}


async def test_unreachable_panel_keeps_the_order_parked(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    order = await _buy(session, cust, plan)
    paid = await wallet_balance(session, shop.id, cust.id)
    await _first_attempt(session, panel, fake, order, created=True)
    _use(panel, _down)
    await _exhaust(session, order.id)
    await session.refresh(order)
    await _settled(session, order)
    assert await lifecycle.reconcile_purchase(session, order.id) == "uncertain"
    assert await lifecycle.reconcile_purchase(session, order.id) == "uncertain"
    await session.refresh(order)
    assert order.status == OrderStatus.PROVISIONING and create_state.parked(order)
    assert create_state.record(order)["checks"] == 2
    assert await wallet_balance(session, shop.id, cust.id) == paid
    # The periodic pass finds it, and the outcome stays the same.
    assert order.id in await lifecycle.parked_purchases(session)
    await worker_jobs.reconcile_purchases()


async def test_a_purchase_that_never_reached_the_create_call_is_refunded_at_once(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    before = await wallet_balance(session, shop.id, cust.id)
    order = await _buy(session, cust, plan)
    _use(panel, _down)  # even the lookup before creating fails: nothing was ever sent
    await _exhaust(session, order.id)
    await session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
    assert create_state.state(order) is None
    assert await wallet_balance(session, shop.id, cust.id) == before


async def test_username_held_by_another_customer_is_left_to_staff(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    order = await _buy(session, cust, plan)
    await _first_attempt(session, panel, fake, order, created=True)
    _use(panel, _down)
    await _exhaust(session, order.id)
    await session.refresh(order)
    other = await factories.customer(session, shop)
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=other.id,
            panel_id=panel.id,
            panel_username=order.params["panel_username"],
            sub_token="tok-other-" + str(order.id),
        )
    )
    await _settled(session, order)
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    assert await lifecycle.reconcile_purchase(session, order.id) == "uncertain"
    await session.refresh(order)
    assert create_state.record(order)["last_check"] == "username_taken"
    assert order.status == OrderStatus.PROVISIONING


async def test_trial_grant_is_kept_while_its_account_is_uncertain(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    trial_cust = await factories.customer(session, shop)
    order = await trials.request(session, trial_cust)
    await session.commit()
    await _first_attempt(session, panel, fake, order, created=False)
    _use(panel, _down)
    await _exhaust(session, order.id)
    grant = await session.scalar(select(TrialGrant).where(TrialGrant.customer_id == trial_cust.id))
    assert grant is not None  # not released: the customer may already have the trial account

    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    await session.refresh(order)
    await _settled(session, order)
    assert await lifecycle.reconcile_purchase(session, order.id) == "refunded"
    grant = await session.scalar(select(TrialGrant).where(TrialGrant.customer_id == trial_cust.id))
    assert grant is None  # proven missing: released, the customer may try again
    refreshed = await session.get(Order, order.id, populate_existing=True)
    assert refreshed is not None and refreshed.status != OrderStatus.PROVISIONING
