"""SEC-F03: an order owns its panel account name durably and adopts only its own account.

Before: a retry or a colliding name adopted any account that already had the name on the panel,
whether made by staff, another bot, an earlier install or another order still in flight. A
customer could be handed someone else's config.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import orders, usernames
from paurubot.db.models import (
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelKind,
    PanelUsernameClaim,
    Service,
)
from paurubot.ledger import wallet
from paurubot.panels import registry
from paurubot.provisioning import claims
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_provisioning import drain


@pytest.fixture(autouse=True)
async def isolate(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    registry._cache.clear()
    yield
    registry.test_transports.clear()
    registry._cache.clear()


async def _setup(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    return shop, panel, plan, cust, fake


def _foreign(fake: FakeTokenPanel, name: str) -> dict:  # type: ignore[type-arg]
    fake.users[name] = {
        "id": 99,
        "username": name,
        "status": "active",
        "used_traffic": 7,
        "data_limit": 123,
        "expire": 0,
        "sub": "someone-elses-sub",
        "request": {},
    }
    return dict(fake.users[name])


async def _buy(session, cust, plan):  # type: ignore[no-untyped-def]
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    return order


async def test_an_existing_foreign_account_is_never_adopted(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)

    # A sequential name has no random tail; an earlier install already used it on this panel.
    async def same_name(*_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return "pb1"

    monkeypatch.setattr(usernames, "generate", same_name)
    before = _foreign(fake, "pb1")
    order = await _buy(session, cust, plan)
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED
    service = await session.get(Service, order.service_id)
    assert service is not None and service.panel_username != "pb1"
    assert service.panel_username.startswith("pb1_")
    assert fake.users["pb1"] == before  # untouched, and not handed to the customer
    assert service.panel_username in fake.users and len(fake.users) == 2


async def test_two_orders_that_pick_the_same_name_get_two_accounts(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    other = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, other.id, 1_000_000, provider="card", payment_id=other.id)

    async def same_name(*_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return "office_ab12"

    monkeypatch.setattr(usernames, "generate", same_name)
    first = await _buy(session, cust, plan)
    second = await _buy(session, other, plan)
    await drain(session, "provision")
    for order in (first, second):
        await session.refresh(order)
        assert order.status == OrderStatus.COMPLETED
    names = {(await session.get(Service, o.service_id)).panel_username for o in (first, second)}  # type: ignore[union-attr]
    assert len(names) == 2 and "office_ab12" in names
    assert len(fake.users) == 2


async def test_claims_are_exclusive_across_concurrent_transactions(engine) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Claims " + secrets.token_hex(3))
        panel = await factories.panel(s, shop)
        plan = await factories.plan(s, shop, panel)
        cust = await factories.customer(s, shop)
        order_ids = []
        for _ in range(6):
            draft = await orders.draft(s, cust, OrderKind.PURCHASE, plan=plan)
            order_ids.append((await orders.create(s, cust, draft)).id)
        await s.commit()
        panel_id = panel.id

    async def take(order_id: int) -> bool:
        async with maker() as s2:
            won = await claims.claim(s2, panel_id, "contested", order_id)
            await asyncio.sleep(0.05)  # hold the claim open while the others try
            await s2.commit()
            return won

    results = await asyncio.gather(*(take(order_id) for order_id in order_ids))
    assert sum(results) == 1
    async with maker() as s3:
        rows = list(
            await s3.scalars(select(PanelUsernameClaim).where(PanelUsernameClaim.panel_id == panel_id))
        )
        assert len(rows) == 1 and rows[0].order_id == order_ids[results.index(True)]


async def test_a_retry_still_adopts_the_account_it_created(session) -> None:  # type: ignore[no-untyped-def]
    import httpx

    shop, panel, plan, cust, fake = await _setup(session)
    order = await _buy(session, cust, plan)
    original = fake.handler

    def lose_create_response(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST" and request.url.path == "/api/user":
            return httpx.Response(502, text="bad gateway")
        return response

    registry.test_transports[panel.id] = httpx.MockTransport(lose_create_response)
    registry._cache.clear()
    await drain(session, "provision")  # first attempt: created, response lost
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    await session.execute(
        update(Job).where(Job.dedupe_key == f"order:{order.id}:provision").values(run_at=Job.created_at)
    )
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == OrderStatus.COMPLETED and len(fake.users) == 1


async def test_orders_named_before_claims_existed_keep_adopting(session) -> None:  # type: ignore[no-untyped-def]
    shop, panel, plan, cust, fake = await _setup(session)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    # In flight across the deploy: the old code stored a name and created the account.
    order.params = {**order.params, "panel_id": panel.id, "panel_username": "legacy_1"}
    await session.commit()
    _foreign(fake, "legacy_1")
    await drain(session, "provision")
    await session.refresh(order)
    service = await session.get(Service, order.service_id)
    assert order.status == OrderStatus.COMPLETED and service is not None
    assert service.panel_username == "legacy_1" and len(fake.users) == 1


async def test_a_refund_before_creating_frees_the_name(session) -> None:  # type: ignore[no-untyped-def]
    from paurubot.provisioning import lifecycle

    shop, panel, plan, cust, fake = await _setup(session)
    order = await _buy(session, cust, plan)
    assert await claims.claim(session, panel.id, "will_free", order.id)
    await session.commit()
    await lifecycle.give_up(session, order.id, "panel down")
    await session.refresh(order)
    assert order.status == OrderStatus.REFUNDED
    rows = await session.scalars(select(PanelUsernameClaim).where(PanelUsernameClaim.order_id == order.id))
    assert list(rows) == []
