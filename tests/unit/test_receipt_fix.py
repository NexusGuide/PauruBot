"""F-23: staff send a card receipt back for a fix instead of rejecting it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.methods import SendMessage

from paurubot.bot.routers.admin import review_kb
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import PaymentStatus
from paurubot.i18n import Texts
from paurubot.notifications import handlers, telegram
from paurubot.payments import engine
from tests import factories
from tests.fakes.telegram import make_bot

CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]


@pytest.fixture
def fake_bot() -> Iterator[Any]:
    bot, recorder = make_bot()
    telegram.set_bot(bot)
    yield recorder
    telegram.set_bot(None)


async def _receipt_under_review(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 100_000)
    await engine.attach_receipt(session, started.payment, file_id="file-1", text="paid 90k", image=None)
    assert started.payment.status == PaymentStatus.VERIFYING
    return shop, cust, started.payment


async def test_fix_request_reopens_payment_for_a_new_receipt(session) -> None:  # type: ignore[no-untyped-def]
    _, _, payment = await _receipt_under_review(session)
    payment.expires_at = utcnow() - timedelta(minutes=5)  # the original deadline already passed
    await session.flush()

    assert await engine.request_fix(session, payment.id, staff_id=None, kind="amount")
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PENDING
    assert payment.receipt_file_id is None and payment.receipt_text is None
    assert payment.expires_at is not None and payment.expires_at > utcnow()  # time to fix it
    history = payment.meta["fix_requests"]
    assert [h["kind"] for h in history] == ["amount"]
    assert history[0]["receipt_file_id"] == "file-1" and history[0]["receipt_text"] == "paid 90k"
    assert engine.fix_count(payment) == 1
    assert payment.amount == 100_000  # nothing about the money changed

    # A second tap (or another reviewer) finds nothing to send back.
    assert not await engine.request_fix(session, payment.id, staff_id=None, kind="card")
    assert await engine.expire_stale(session) == 0  # the fresh deadline keeps it open

    # The customer sends the corrected receipt: it is under review again.
    await engine.attach_receipt(session, payment, file_id="file-2", text=None, image=None)
    assert payment.status == PaymentStatus.VERIFYING
    assert payment.receipt_file_id == "file-2"
    assert engine.fix_count(payment) == 1


async def test_corrected_receipt_is_never_auto_approved(session) -> None:  # type: ignore[no-untyped-def]
    _, cust, payment = await _receipt_under_review(session)
    assert await engine.request_fix(session, payment.id, staff_id=None, kind="card")
    cust.card_trust = "trusted"  # would normally skip review entirely
    await session.flush()
    await engine.attach_receipt(session, payment, file_id="file-2", text=None, image=None)
    assert payment.status == PaymentStatus.VERIFYING  # a human looks at it again


async def test_fix_request_only_for_card_receipts_under_review(session) -> None:  # type: ignore[no-untyped-def]
    _, _, payment = await _receipt_under_review(session)
    with pytest.raises(ValueError):
        await engine.request_fix(session, payment.id, staff_id=None, kind="other")
    assert await engine.reject(session, payment.id, staff_id=None, note="no")
    assert not await engine.request_fix(session, payment.id, staff_id=None, kind="amount")
    assert not await engine.request_fix(session, 10**9, staff_id=None, kind="amount")


async def test_customer_is_told_what_to_fix(session, fake_bot) -> None:  # type: ignore[no-untyped-def]
    _, cust, payment = await _receipt_under_review(session)
    assert await engine.request_fix(session, payment.id, staff_id=None, kind="card")
    job = SimpleNamespace(payload={"payment_id": payment.id, "kind": "card"})
    await handlers.payment_fix_requested(session, job)  # type: ignore[arg-type]
    sent = fake_bot.last(SendMessage)
    assert sent.chat_id == cust.telegram_id
    assert str(payment.id) in sent.text
    assert [data for _, data in fake_bot.buttons(sent)] == [f"rc:resend:{payment.id}"]

    # Once the new receipt is in, a late job stays quiet.
    fake_bot.reset()
    await engine.attach_receipt(session, payment, file_id="file-2", text=None, image=None)
    await handlers.payment_fix_requested(session, job)  # type: ignore[arg-type]
    assert not fake_bot.calls


def test_fix_buttons_only_on_card_reviews() -> None:
    t = Texts("en")
    card = [b.callback_data for row in review_kb(t, 5, fixable=True).inline_keyboard for b in row]
    assert card == ["rv:ok:5", "rv:no:5", "rv:fixa:5", "rv:fixc:5"]
    other = [b.callback_data for row in review_kb(t, 5).inline_keyboard for b in row]
    assert other == ["rv:ok:5", "rv:no:5"]
