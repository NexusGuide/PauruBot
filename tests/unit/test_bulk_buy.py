"""Buying several services of one plan in one payment (CAT-07)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.auth import rbac
from paurubot.core import bulk, orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, OrderStatus, PanelKind, ResellerProfile, Service
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import handlers as _handlers  # noqa: F401  (registers the notify queue)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot
from tests.unit.test_service_lifecycle import drain


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN, inbounds=["vless:VLESS TCP"])
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    session.add(
        ResellerProfile(shop_id=shop.id, customer_id=cust.id, permissions=sorted(rbac.DEFAULT_FOR_RESELLER))
    )
    await wallet.topup(session, shop.id, cust.id, 450_000, provider="card", payment_id=cust.id)
    await session.commit()
    await session.refresh(cust, ["reseller_profile"])
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    yield {"shop": shop, "plan": plan, "cust": cust, "fake": fake, "tg": tg}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def test_bulk_buy_pays_once_and_sends_one_file(session, env) -> None:  # type: ignore[no-untyped-def]
    cust, plan = env["cust"], env["plan"]
    batch, made = await bulk.buy_many(session, cust, plan, 3, "Ali")
    await session.commit()
    assert len(made) == 3 and all(o.status == OrderStatus.PAID for o in made)
    assert await wallet_balance(session, env["shop"].id, cust.id) == 150_000
    await drain(session, "provision")
    await drain(session, "notify")
    services = list(await session.scalars(select(Service).where(Service.customer_id == cust.id)))
    assert sorted(s.label for s in services) == ["Ali 1", "Ali 2", "Ali 3"]
    files = [c for c in env["tg"].calls if type(c).__name__ == "SendDocument"]
    assert len(files) == 1  # one summary for the batch, not one message per service
    rows, finished = await bulk.progress(session, cust.id, batch)
    assert finished and len(rows) == 3


async def test_bulk_rules(session, env) -> None:  # type: ignore[no-untyped-def]
    cust, plan = env["cust"], env["plan"]
    with pytest.raises(orders.OrderError, match="insufficient_balance"):
        await bulk.buy_many(session, cust, plan, 5)
    with pytest.raises(orders.OrderError, match="bulk_count"):
        await bulk.buy_many(session, cust, plan, 1)
    await shop_settings.set_many(session, env["shop"].id, {"bulk_buy_max": 3})
    with pytest.raises(orders.OrderError, match="bulk_count"):
        await bulk.buy_many(session, cust, plan, 4)
    cust.reseller_profile.max_active_services = 2
    with pytest.raises(orders.OrderError, match="reseller_service_limit"):
        await bulk.buy_many(session, cust, plan, 3)
    stranger = await factories.customer(session, env["shop"])
    assert bulk.allowed(await shop_settings.load(session, env["shop"].id), stranger) == 0
    await session.rollback()
