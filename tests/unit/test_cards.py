"""Card-to-card safety (PAY-17, PAY-18): trusted and blocked customers, registered cards, which
card is shown, a random tail on the amount and approving receipts nobody reviewed in time."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from paurubot.core import cards
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments.base import PaymentError
from tests import factories

CARDS = [
    {"number": "6037991234567890", "holder": "A", "bank": "Melli"},
    {"number": "6104337712345678", "holder": "B", "bank": "Mellat"},
]
VALID = "6037991234567893"  # passes the Luhn check


@pytest.fixture
async def shop(session):  # type: ignore[no-untyped-def]
    row = await factories.shop(session, "Cards")
    await shop_settings.set_many(session, row.id, {"card_accounts": CARDS})
    await session.commit()
    return row


def test_card_numbers_and_picking() -> None:
    assert cards.luhn_ok(VALID) and not cards.luhn_ok("6037991234567890")
    assert cards.digits("۶۰۳۷-۹۹۱۲ 3456 7893") == VALID
    assert cards.masked(VALID) == "6037-99**-****-7893"
    accounts = [{"number": str(i)} for i in range(3)]
    assert cards.pick_accounts(accounts, "all", 7) == accounts
    assert cards.pick_accounts(accounts, "rotate", 7) == [accounts[1]]
    assert cards.pick_accounts(accounts, "random", 7)[0] in accounts
    for _ in range(50):
        tailed = cards.random_tail(100_000, 10_000_000)
        assert 100_001 <= tailed <= 100_999
    assert cards.random_tail(100_000, 100_000) == 100_000  # never past the cap


async def test_one_card_shown_and_random_tail(session, shop) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, shop.id, {"card_pick": "rotate", "card_random_tail": True})
    cust = await factories.customer(session, shop)
    started = await engine.start(session, cust, "card", 100_000)
    shown = started.checkout.instructions["accounts"]
    assert len(shown) == 1 and started.payment.meta["card"] == shown[0]["number"][-4:]
    assert 100_001 <= started.payment.amount <= 100_999
    assert started.checkout.instructions["amount"] == started.payment.amount


async def test_blocked_and_trusted_customers(session, shop) -> None:  # type: ignore[no-untyped-def]
    blocked = await factories.customer(session, shop)
    blocked.card_trust = "blocked"
    assert "card" not in await engine.available(session, shop.id, blocked)
    with pytest.raises(PaymentError) as exc:
        await engine.start(session, blocked, "card", 100_000)
    assert exc.value.key == "card_blocked"
    trusted = await factories.customer(session, shop)
    trusted.card_trust = "trusted"
    started = await engine.start(session, trusted, "card", 5_000_000)  # trust must not bypass the cap
    await engine.attach_receipt(session, started.payment, file_id="f", text=None, image=None)
    await session.refresh(started.payment)
    assert started.payment.status == PaymentStatus.VERIFYING
    await shop_settings.set_many(
        session,
        shop.id,
        {"card_auto_approve": True, "card_auto_max_amount": 300_000, "card_auto_min_history": 999},
    )
    within_cap = await engine.start(session, trusted, "card", 300_000)
    await engine.attach_receipt(session, within_cap.payment, file_id="within-cap", text=None, image=None)
    await session.refresh(within_cap.payment)
    assert within_cap.payment.status == PaymentStatus.PAID  # only history is waived


async def test_card_must_be_registered_and_approved(session, shop) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, shop.id, {"card_verify_enabled": True, "card_verify_min_amount": 50_000}
    )
    cust = await factories.customer(session, shop)
    await engine.start(session, cust, "card", 20_000)  # below the threshold
    with pytest.raises(PaymentError) as exc:
        await engine.start(session, cust, "card", 100_000)
    assert exc.value.key == "card_verify_required"
    with pytest.raises(cards.CardError):
        await cards.register(session, cust, "6037991234567890", "photo")  # fails the Luhn check
    card, other = await cards.register(session, cust, VALID, "photo")
    assert other is None and card.status == "pending" and card.last4 == "7893"
    with pytest.raises(cards.CardError) as pending:
        await cards.register(session, cust, VALID, "photo")
    assert pending.value.key == "card_verify_pending"
    assert await cards.review(session, card, True, None)
    assert not await cards.review(session, card, False, None)  # decided once
    await engine.start(session, cust, "card", 100_000)
    # The same card registered by someone else is flagged to staff.
    stranger = await factories.customer(session, shop)
    _, flagged = await cards.register(session, stranger, VALID, "photo")
    assert flagged is not None and flagged.id == cust.id


async def test_late_receipts_approved_within_the_cap(session, shop) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session, shop.id, {"card_auto_approve_after_minutes": 30, "card_auto_max_amount": 300_000}
    )
    cust = await factories.customer(session, shop)
    small = await engine.start(session, cust, "card", 100_000)
    big = await engine.start(session, cust, "card", 900_000)
    fresh = await engine.start(session, cust, "card", 100_000)
    for started in (small, big, fresh):
        await engine.attach_receipt(
            session, started.payment, file_id=f"f{started.payment.id}", text=None, image=None
        )
    old = (utcnow() - timedelta(minutes=45)).isoformat()
    for started in (small, big):
        started.payment.meta = {**started.payment.meta, "receipt_at": old}
    await session.commit()
    assert await cards.approve_late(session) == 1
    statuses = {
        p.id: p.status
        for p in await session.scalars(
            select(Payment).where(Payment.id.in_([small.payment.id, big.payment.id, fresh.payment.id]))
        )
    }
    assert statuses[small.payment.id] == PaymentStatus.PAID
    assert statuses[big.payment.id] == PaymentStatus.VERIFYING  # above the cap: a person decides
    assert statuses[fresh.payment.id] == PaymentStatus.VERIFYING  # not late yet
    assert await session.scalar(select(Job).where(Job.kind == "fulfill")) is not None
