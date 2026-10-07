from __future__ import annotations

import pytest
from aiogram.methods import CopyMessage, SendMessage, SendPhoto
from sqlalchemy import select, update

from paurubot.core import orders
from paurubot.db.models import Broadcast, Customer, Job, JobStatus, OrderKind, Service
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import handlers  # noqa: F401  (registers queues)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.worker import (
    jobs,  # noqa: F401
    queue,
)
from paurubot.worker.runner import QUEUES, run_job
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot

FA = Texts("fa")


async def drain(session, name: str) -> int:  # type: ignore[no-untyped-def]
    count = 0
    while claimed := await queue.claim(session, name, limit=1):
        await run_job(name, QUEUES[name], claimed[0])
        count += 1
    return count


@pytest.fixture
async def tg(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    yield fake
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def test_purchase_sends_service_card_and_qr(session, tg) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    plan = await factories.plan(session, shop, panel)
    cust = await factories.customer(session, shop, telegram_id=5501)
    await wallet.topup(session, shop.id, cust.id, 200_000, provider="card", payment_id=5501)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    assert await drain(session, "notify") >= 1
    texts = tg.texts(5501)
    assert any(FA("service_ready") in text for text in texts)
    photo = tg.last(SendPhoto)
    assert photo.chat_id == 5501
    # the info card carries the QR, so it replaces the bare QR picture (BOT-06)
    assert photo.photo.filename == "card.png" and photo.photo.data.startswith(b"\x89PNG")
    assert photo.caption.startswith("🪪")
    assert any("s:card" in (cb or "") for _, cb in tg.buttons(tg.last(SendMessage)))


async def test_purchase_sends_bare_qr_when_cards_are_off(session, tg) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import settings as shop_settings

    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"info_card_enabled": False})
    panel = await factories.panel(session, shop)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    plan = await factories.plan(session, shop, panel)
    cust = await factories.customer(session, shop, telegram_id=5502)
    await wallet.topup(session, shop.id, cust.id, 200_000, provider="card", payment_id=5502)
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await drain(session, "notify")
    photo = tg.last(SendPhoto)
    assert photo.photo.filename == "qr.png" and photo.caption == FA("qr_caption")
    assert not any("s:card" in (cb or "") for _, cb in tg.buttons(tg.last(SendMessage)))


async def test_blocked_customer_is_marked_and_skipped_by_broadcast(session, tg) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    good = await factories.customer(session, shop, telegram_id=6601)
    blocked = await factories.customer(session, shop, telegram_id=6602)
    tg.blocked.add(6602)
    row = Broadcast(shop_id=shop.id, source_chat_id=1, source_message_id=2, status=JobStatus.QUEUED)
    session.add(row)
    await session.flush()
    await queue.enqueue(session, "broadcast", "run", {"broadcast_id": row.id}, shop_id=shop.id)
    await session.commit()
    await drain(session, "broadcast")
    await session.refresh(row)
    assert (row.status, row.sent, row.failed) == (JobStatus.DONE, 1, 1)
    copies = [c.chat_id for c in tg.calls if isinstance(c, CopyMessage)]
    assert copies == [6601, 6602]
    await session.refresh(blocked)
    assert blocked.blocked_bot_at is not None
    assert isinstance(blocked, Customer)
    assert good.blocked_bot_at is None


async def test_service_alert_has_renew_button(session, tg) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    cust = await factories.customer(session, shop, telegram_id=7701)
    service = Service(
        shop_id=shop.id, customer_id=cust.id, panel_id=panel.id, panel_username="u_alert", label="Home"
    )
    session.add(service)
    await session.flush()
    await queue.enqueue(
        session,
        "notify",
        "service_alert",
        {"service_id": service.id, "alert": "expiring", "days": 2},
        shop_id=shop.id,
    )
    await session.commit()
    await drain(session, "notify")
    sent = tg.last(SendMessage)
    assert "Home" in sent.text and "2" in sent.text
    assert any("s:renew" in (cb or "") for _, cb in tg.buttons(sent))
    assert await session.scalar(select(Service).where(Service.id == service.id)) is not None
