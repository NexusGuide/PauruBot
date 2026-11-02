"""Customer extras through the real dispatcher: welcome gift, sending balance, reporting a problem."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select

from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Svc, Wallet
from paurubot.core import settings as shop_settings
from paurubot.db.models import Service, ServiceStatus, Ticket, TicketMessage
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import GB
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
UID = 7701


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Extras bot")
    panel = await factories.panel(session, shop)
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def test_welcome_gift_once_and_only_for_invited(session, env) -> None:  # type: ignore[no-untyped-def]
    send, shop = env["send"], env["shop"]
    await shop_settings.set_many(session, shop.id, {"start_gift_amount": 5_000})
    await session.commit()
    await send(message_update(UID, "/start"))  # not invited: no gift by default
    inviter = await _customer(session, shop.id, UID)
    assert await wallet_balance(session, shop.id, inviter.id) == 0
    await send(message_update(UID + 1, f"/start ref_{UID}"))
    await send(message_update(UID + 1, "/start"))
    invited = await _customer(session, shop.id, UID + 1)
    assert invited.referrer_id == inviter.id
    assert await wallet_balance(session, shop.id, invited.id) == 5_000
    assert FA("start_gift", amount="5,000") in env["fake"].texts(UID + 1)


async def test_send_balance_from_the_wallet(session, env) -> None:  # type: ignore[no-untyped-def]
    send, shop, fake = env["send"], env["shop"], env["fake"]
    await shop_settings.set_many(session, shop.id, {"wallet_transfer_enabled": True})
    await session.commit()
    await send(message_update(UID + 2, "/start"))
    await send(message_update(UID + 3, "/start"))
    sender = await _customer(session, shop.id, UID + 2)
    receiver = await _customer(session, shop.id, UID + 3)
    await wallet.topup(session, shop.id, sender.id, 100_000, provider="card", payment_id=sender.id)
    await session.commit()
    await send(message_update(UID + 2, FA("btn_wallet")))
    assert FA("btn_transfer") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID + 2, Wallet(action="transfer").pack()))
    await send(message_update(UID + 2, str(UID + 3)))
    await send(message_update(UID + 2, "30000"))
    confirm = fake.last(SendMessage)
    go = next(data for _, data in fake.buttons(confirm) if data and "transfer_go" in data)
    await send(callback_update(UID + 2, go))
    await send(callback_update(UID + 2, go))  # pressed twice: still one transfer
    assert await wallet_balance(session, shop.id, sender.id) == 70_000
    assert await wallet_balance(session, shop.id, receiver.id) == 30_000


async def test_report_a_problem_opens_a_ticket(session, env) -> None:  # type: ignore[no-untyped-def]
    send, shop, panel, fake = env["send"], env["shop"], env["panel"], env["fake"]
    await send(message_update(UID + 4, "/start"))
    customer = await _customer(session, shop.id, UID + 4)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="u_broken",
        label="Phone",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * GB,
    )
    panel.is_healthy = False
    session.add(service)
    await session.commit()
    await send(callback_update(UID + 4, Svc(action="report", id=service.id).pack()))
    ticket = await session.scalar(select(Ticket).where(Ticket.customer_id == customer.id))
    assert ticket is not None and ticket.source == "report"
    # REF-09: the ticket names its service, marked as not read live (the panel is down here).
    assert ticket.service_id == service.id
    assert ticket.service_context["id"] == service.id and ticket.service_context["panel_down"] is True
    note = await session.scalar(select(TicketMessage).where(TicketMessage.ticket_id == ticket.id))
    assert note is not None and "u_broken" in (note.text or "")
    assert fake.last(EditMessageText).text == FA("report_known_outage", id=ticket.id)
    # A second report right away is held back.
    await send(callback_update(UID + 4, Svc(action="report", id=service.id).pack()))
    assert len(list(await session.scalars(select(Ticket).where(Ticket.customer_id == customer.id)))) == 1
