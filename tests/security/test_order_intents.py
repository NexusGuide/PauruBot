"""Order intent replay includes account/custom bodies without invoking repricing twice."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from paurubot.db.base import utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import order_intents
from paurubot.core.orders import OrderError
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus
from tests import factories
from tests.security import test_payment_intents as fixtures

intent_engine = fixtures.intent_engine

pytestmark = pytest.mark.security
KEY = "order-request-123"
BODY = {"kind": "purchase", "plan_id": 1, "label": "A"}


async def make_order(session, customer):  # type: ignore[no-untyped-def]
    order = Order(shop_id=customer.shop_id, customer_id=customer.id, kind=OrderKind.PURCHASE,
                  amount=90_000, base_price=90_000, params={"label": "A"},
                  expires_at=utcnow() + timedelta(minutes=15), source="webapp")
    session.add(order)
    await session.flush()
    return order


async def test_retry_does_not_reprice_and_hides_raw_key(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    async def build():  # type: ignore[no-untyped-def]
        return await make_order(session, customer)
    create = AsyncMock(side_effect=build)
    first = await order_intents.create_once(session, customer, KEY, BODY, create)
    await session.commit()
    second = await order_intents.create_once(session, customer, KEY, dict(reversed(list(BODY.items()))), create)
    assert second.id == first.id and second.amount == 90_000
    assert second.params["label"] == "A"
    assert KEY not in str(second.params)
    create.assert_awaited_once()


async def test_changed_body_conflicts_even_when_closed(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    async def build():  # type: ignore[no-untyped-def]
        return await make_order(session, customer)
    order = await order_intents.create_once(session, customer, KEY, BODY, build)
    order.status = OrderStatus.CANCELED
    await session.flush()
    create = AsyncMock()
    with pytest.raises(OrderError, match="order_intent_conflict"):
        await order_intents.create_once(session, customer, KEY, {**BODY, "label": "B"}, create)
    create.assert_not_awaited()


@pytest.mark.parametrize("state", ["expired", "canceled", "failed", "refunded"])
async def test_closed_key_never_creates_replacement(session, state) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    async def build():  # type: ignore[no-untyped-def]
        return await make_order(session, customer)
    order = await order_intents.create_once(session, customer, KEY, BODY, build)
    if state == "expired":
        order.expires_at = utcnow() - timedelta(seconds=1)
    else:
        order.status = OrderStatus(state)
    await session.flush()
    create = AsyncMock()
    with pytest.raises(OrderError, match="order_intent_closed"):
        await order_intents.create_once(session, customer, KEY, BODY, create)
    create.assert_not_awaited()


@pytest.mark.parametrize("state", ["paid", "provisioning", "completed"])
async def test_paid_replay_reads_original_order(session, state) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    async def build():  # type: ignore[no-untyped-def]
        return await make_order(session, customer)
    order = await order_intents.create_once(session, customer, KEY, BODY, build)
    order.status = OrderStatus(state)
    order.expires_at = utcnow() - timedelta(days=1)
    await session.flush()
    create = AsyncMock()
    assert (await order_intents.create_once(session, customer, KEY, BODY, create)).id == order.id
    create.assert_not_awaited()


@pytest.mark.parametrize("key", ["", "short", "x" * 129, "wrong key", "کلیدنامعتبر"])
async def test_invalid_key_rejected_without_writes(session, key) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    customer = await factories.customer(session, shop)
    create = AsyncMock()
    with pytest.raises(OrderError, match="order_intent_invalid"):
        await order_intents.create_once(session, customer, key, BODY, create)
    create.assert_not_awaited()


async def test_customer_and_shop_scope_do_not_reuse_orders(session) -> None:  # type: ignore[no-untyped-def]
    a = await factories.shop(session)
    b = await factories.shop(session)
    people = [await factories.customer(session, a), await factories.customer(session, a), await factories.customer(session, b)]
    ids = []
    for customer in people:
        async def build(owner=customer):  # type: ignore[no-untyped-def]
            return await make_order(session, owner)
        ids.append((await order_intents.create_once(session, customer, KEY, BODY, build)).id)
    assert len(set(ids)) == 3


@pytest.mark.parametrize("conflicting", [False, True])
async def test_separate_sessions_serialize_creation(intent_engine, conflicting) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(intent_engine, expire_on_commit=False)
    async with maker() as session:
        shop = await factories.shop(session)
        customer = await factories.customer(session, shop)
        cid = customer.id
        await session.commit()
    key = f"concurrent-{uuid4().hex}"
    gate = asyncio.Barrier(4)
    calls = []
    async def request(i):  # type: ignore[no-untyped-def]
        async with maker() as session:
            customer = await session.get(Customer, cid)
            await gate.wait()
            async def build():  # type: ignore[no-untyped-def]
                calls.append(i)
                return await make_order(session, customer)
            try:
                result = await order_intents.create_once(session, customer, key, {**BODY, "label": str(i) if conflicting else "A"}, build)
                await session.commit()
                return result.id
            except OrderError as exc:
                await session.rollback()
                return exc.key
    outcomes = await asyncio.wait_for(asyncio.gather(*(request(i) for i in range(4))), timeout=20)
    assert len(calls) == 1
    assert len({x for x in outcomes if isinstance(x, int)}) == 1
    assert outcomes.count("order_intent_conflict") == (3 if conflicting else 0)
    async with maker() as session:
        assert await session.scalar(select(func.count(Order.id)).where(Order.customer_id == cid)) == 1
