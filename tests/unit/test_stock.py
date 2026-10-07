"""Selling ready-made configs from a stock (CAT-05)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.core import catalog, orders, stock
from paurubot.db.models import Job, JobStatus, Location, Order, OrderKind, OrderStatus, Plan, StockItem
from paurubot.ledger import wallet
from paurubot.notifications import handlers as _handlers  # noqa: F401  (registers the notify queue)
from paurubot.notifications import telegram as notify_telegram
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.telegram import make_bot
from tests.unit.test_service_lifecycle import _buy, drain

CONFIGS = "vless://a@h1:443?type=tcp#one\nvless://b@h2:443?type=tcp#two\nvless://a@h1:443?type=tcp#one\n"


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    loc = Location(shop_id=shop.id, title="Ready")
    session.add(loc)
    await session.flush()
    plan = Plan(shop_id=shop.id, title="Ready 30d", price=50_000, location_id=loc.id, delivery="stock")
    session.add(plan)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 500_000, provider="card", payment_id=cust.id)
    await session.commit()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    yield {"shop": shop, "plan": plan, "cust": cust, "tg": tg}
    notify_telegram.set_bot(None)


def test_split() -> None:
    assert stock.split("a\nb\n\n") == ["a", "b"]
    assert stock.split("line1\nline2\n\nline3\n") == ["line1\nline2", "line3"]
    assert stock.split("  \n") == []


async def test_stock_is_encrypted_deduplicated_and_sold_once(session, env) -> None:  # type: ignore[no-untyped-def]
    plan, cust, shop = env["plan"], env["cust"], env["shop"]
    assert await catalog.get_plan(session, shop.id, plan.id, cust) is None  # empty stock: not on sale
    result = await stock.add(session, plan, CONFIGS)
    assert (result.added, result.duplicates) == (2, 1)
    again = await stock.add(session, plan, "vless://b@h2:443?type=tcp#two")
    assert again.added == 0 and again.duplicates == 1
    raw = list(await session.scalars(select(StockItem.payload_enc).where(StockItem.plan_id == plan.id)))
    assert all("vless://" not in r for r in raw)
    await session.commit()
    assert await catalog.get_plan(session, shop.id, plan.id, cust) is not None

    first = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    second = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    for order in (first, second):
        await session.refresh(order)
        assert order.status == OrderStatus.COMPLETED and order.service_id is None
    given = {
        stock.payload(await session.get(StockItem, int(o.params["stock_item_id"])))  # type: ignore[arg-type]
        for o in (first, second)
    }
    assert given == {"vless://a@h1:443?type=tcp#one", "vless://b@h2:443?type=tcp#two"}
    await drain(session, "notify")
    assert any("vless://" in text for text in env["tg"].texts(cust.telegram_id))
    with pytest.raises(orders.OrderError, match="out_of_stock|plan_unavailable"):
        await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)


async def test_sold_out_after_payment_refunds(session, env) -> None:  # type: ignore[no-untyped-def]
    plan, cust = env["plan"], env["cust"]
    await stock.add(session, plan, "vless://only@h:443#x")
    await session.commit()
    order = await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await session.execute(update(StockItem).where(StockItem.plan_id == plan.id).values(status=stock.DISABLED))
    await session.commit()
    await drain(session, "provision")
    row = await session.get(Order, order.id, populate_existing=True)
    assert row is not None and row.status in {OrderStatus.FAILED, OrderStatus.REFUNDED}


async def test_two_takers_never_share_an_item(session, env) -> None:  # type: ignore[no-untyped-def]
    plan, cust = env["plan"], env["cust"]
    await stock.add(session, plan, "vless://solo@h:443#x")
    await session.commit()
    a = Order(shop_id=cust.shop_id, customer_id=cust.id, kind=OrderKind.PURCHASE, plan_id=plan.id, amount=0)
    b = Order(shop_id=cust.shop_id, customer_id=cust.id, kind=OrderKind.PURCHASE, plan_id=plan.id, amount=0)
    session.add_all([a, b])
    await session.flush()
    got = [await stock.take(session, a), await stock.take(session, b)]
    assert got[0] is not None and got[1] is None
    assert await stock.take(session, a) is got[0]  # retry returns the same item
