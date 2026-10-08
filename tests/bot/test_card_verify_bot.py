"""Registering a card before paying card to card, through the real dispatcher (PAY-18)."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto
from sqlalchemy import select, update

import paurubot.notifications.handlers  # noqa: F401  (registers the notify queue)
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin, Pay, Wallet
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, StaffRole, VerifiedCard
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update, photo_update
from tests.unit.test_resellers import drain

FA = Texts("fa")
UID = 7901
OWNER = 7902


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Card verify")
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "card_accounts": [{"number": "6037991234567890", "holder": "A", "bank": "Melli"}],
            "card_verify_enabled": True,
        },
    )
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="cv_owner", password=None, telegram_id=OWNER
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def test_register_card_then_pay(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(UID, "/start"))
    await send(message_update(OWNER, "/start"))
    await send(callback_update(UID, Pay(method="card", amount=100_000).pack()))
    assert fake.last(SendMessage).text == FA("card_verify_required")
    await send(callback_update(UID, Wallet(action="cardv").pack()))
    await send(message_update(UID, "1234"))
    assert FA("card_number_invalid") in fake.texts(UID)
    await send(message_update(UID, "6037-9912-3456-7893"))
    await send(photo_update(UID, "card-photo"))
    card = await session.scalar(select(VerifiedCard))
    assert card is not None and card.status == "pending" and card.photo_file_id == "card-photo"
    await drain(session, "notify")
    review = fake.last(SendPhoto)
    assert review.chat_id == OWNER and "6037-99**-****-7893" in review.caption
    await send(callback_update(OWNER, Admin(section="cardv", action="ok", id=card.id).pack()))
    await session.refresh(card)
    assert card.status == "approved"
    await drain(session, "notify")
    assert FA("card_verify_approved", card="6037-99**-****-7893") in fake.texts(UID)
    # Now card to card opens, with one-tap copy buttons.
    await send(callback_update(UID, Pay(method="card", amount=100_000).pack()))
    instructions = next(
        c for c in reversed(fake.calls) if isinstance(c, SendMessage) and "6037991234567890" in (c.text or "")
    )
    copies = [b.copy_text.text for row in instructions.reply_markup.inline_keyboard for b in row]
    assert "6037991234567890" in copies and "100000" in copies
    customer = await _customer(session, shop.id, UID)
    assert customer.card_trust == "normal"
