"""Paid membership, reminders off and deleting a service, through the real dispatcher."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage

from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Rs, Svc
from paurubot.core import settings as shop_settings
from paurubot.db.models import ResellerGroup, Service, ServiceStatus
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import GB
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
UID = 8101


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Sales bot")
    panel = await factories.panel(session, shop)
    group = ResellerGroup(shop_id=shop.id, title="Gold", discount_percent=20)
    session.add(group)
    await session.flush()
    await shop_settings.set_many(
        session, shop.id, {"reseller_membership_price": 300_000, "reseller_membership_group_id": group.id}
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def test_buy_membership_from_the_bot(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(UID, "/start"))
    customer = await _customer(session, shop.id, UID)
    await wallet.topup(session, shop.id, customer.id, 400_000, provider="card", payment_id=customer.id)
    await session.commit()
    await send(message_update(UID, FA("btn_reseller")))
    assert "Gold" in fake.last(SendMessage).text
    await send(callback_update(UID, Rs(action="member").pack()))
    pay = next(
        data for text, data in fake.buttons(fake.last(EditMessageText)) if text == FA("btn_pay_wallet")
    )
    await send(callback_update(UID, pay))
    await session.refresh(customer, ["reseller_profile"])
    assert customer.is_reseller and customer.reseller_profile.group.title == "Gold"
    await send(message_update(UID, FA("btn_reseller")))  # now the reseller panel opens
    assert FA("rs_btn_search") in [text for text, _ in fake.buttons(fake.last(SendMessage))]


async def test_reminders_off_and_delete_service(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel = env["send"], env["fake"], env["shop"], env["panel"]
    await shop_settings.set_many(session, shop.id, {"self_delete_enabled": True})
    await send(message_update(UID + 1, "/start"))
    customer = await _customer(session, shop.id, UID + 1)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="u_gone",
        status=ServiceStatus.ACTIVE,
        data_limit=GB,
    )
    session.add(service)
    await session.commit()
    await send(message_update(UID + 1, FA("btn_services")))
    assert FA("btn_alerts_off") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID + 1, Svc(action="mute").pack()))
    await session.refresh(customer)
    assert customer.alerts_muted
    await send(callback_update(UID + 1, Svc(action="cancel", id=service.id).pack()))
    assert FA("cancel_service_confirm", name="u_gone") == fake.last(EditMessageText).text
