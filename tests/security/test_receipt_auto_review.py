"""All automatic receipt paths must respect human-review safety exclusions."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from paurubot.core import cards
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Job, PaymentStatus
from paurubot.payments import engine
from tests import factories

pytestmark = pytest.mark.security
CARDS = [{"number": "6037991234567890", "holder": "Test", "bank": "Melli"}]


async def pending_receipt(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"card_accounts": CARDS})
    customer = await factories.customer(session, shop)
    started = await engine.start(session, customer, "card", 100_000)
    return shop, customer, started.payment


@pytest.mark.parametrize("trust,risk", [
    ("trusted", "medium"), ("trusted", "high"), ("blocked", "low"),
    ("blocked", "high"), ("normal", "medium"), ("normal", "high"),
])
async def test_changed_trust_or_risk_cannot_auto_approve(session, trust, risk) -> None:  # type: ignore[no-untyped-def]
    shop, customer, payment = await pending_receipt(session)
    await shop_settings.set_many(session, shop.id, {
        "card_auto_approve": True, "card_auto_max_amount": 300_000, "card_auto_min_history": 0,
    })
    # Start was legal; the customer's policy changed before the receipt arrived.
    customer.card_trust, customer.risk = trust, risk
    await engine.attach_receipt(session, payment, file_id="receipt", text=None)
    assert payment.status == PaymentStatus.VERIFYING
    assert not await engine.auto_approvable(session, payment, await shop_settings.load(session, shop.id))
    assert await session.scalar(select(func.count(Job.id)).where(Job.kind == "fulfill")) == 0


async def test_corrected_receipt_stays_manual_even_after_timeout(session) -> None:  # type: ignore[no-untyped-def]
    shop, customer, payment = await pending_receipt(session)
    await engine.attach_receipt(session, payment, file_id="first", text=None)
    assert await engine.request_fix(session, payment.id, staff_id=None, kind="amount")
    customer.card_trust = "trusted"
    await engine.attach_receipt(session, payment, file_id="corrected", text=None)
    assert payment.status == PaymentStatus.VERIFYING
    await shop_settings.set_many(session, shop.id, {"card_auto_approve_after_minutes": 30})
    payment.meta = {**payment.meta, "receipt_at": (utcnow() - timedelta(hours=1)).isoformat()}
    await session.commit()
    assert await cards.approve_late(session) == 0
    await session.refresh(payment)
    assert payment.status == PaymentStatus.VERIFYING
    assert await session.scalar(select(func.count(Job.id)).where(Job.kind == "fulfill")) == 0


@pytest.mark.parametrize("receipt_at", ["bad-date", "2020-01-01T00:00:00", "999999999999999", 1])
async def test_bad_receipt_timestamp_does_not_poison_other_payments(session, receipt_at) -> None:  # type: ignore[no-untyped-def]
    shop, customer, broken = await pending_receipt(session)
    good = (await engine.start(session, customer, "card", 100_000)).payment
    await shop_settings.set_many(session, shop.id, {"card_auto_approve_after_minutes": 30})
    for payment in (broken, good):
        await engine.attach_receipt(session, payment, file_id=f"receipt-{payment.id}", text=None)
    broken.meta = {**broken.meta, "receipt_at": receipt_at}
    good.meta = {**good.meta, "receipt_at": (utcnow() - timedelta(hours=1)).isoformat()}
    await session.commit()
    assert await cards.approve_late(session) == 1
    await session.refresh(broken)
    await session.refresh(good)
    assert broken.status == PaymentStatus.VERIFYING
    assert good.status == PaymentStatus.PAID
    assert await cards.approve_late(session) == 0  # no second credit/fulfillment


async def test_trusted_clean_policy_and_normal_auto_approval_are_preserved(session) -> None:  # type: ignore[no-untyped-def]
    shop, customer, normal = await pending_receipt(session)
    customer.card_trust = "trusted"
    payment = (await engine.start(session, customer, "card", 5_000_000)).payment
    await engine.attach_receipt(session, payment, file_id="trusted", text=None)
    assert payment.status == PaymentStatus.PAID  # PAY-17 exception remains explicit
    customer.card_trust = "normal"
    await shop_settings.set_many(session, shop.id, {
        "card_auto_approve": True, "card_auto_min_history": 0, "card_auto_max_amount": 300_000,
    })
    await engine.attach_receipt(session, normal, file_id="normal", text=None)
    assert normal.status == PaymentStatus.PAID


@pytest.mark.parametrize("case", [
    "provider", "pending", "missing_receipt", "duplicate", "correction", "missing_customer", "wrong_shop",
])
async def test_shared_guard_rejects_ineligible_resources(case: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    payment = SimpleNamespace(
        provider="card", status=PaymentStatus.VERIFYING, receipt_file_id="receipt",
        duplicate_of_id=None, meta={}, customer_id=42, shop_id=7,
    )
    customer = SimpleNamespace(shop_id=7, card_trust="trusted", risk="low")
    if case == "provider":
        payment.provider = "zarinpal"
    elif case == "pending":
        payment.status = PaymentStatus.PENDING
    elif case == "missing_receipt":
        payment.receipt_file_id = None
    elif case == "duplicate":
        payment.duplicate_of_id = 99
    elif case == "correction":
        payment.meta = {"fix_requests": [{"kind": "amount"}]}
    elif case == "missing_customer":
        customer = None
    elif case == "wrong_shop":
        customer.shop_id = 8
    session = AsyncMock()
    session.get.return_value = customer
    assert not await engine.auto_approvable(session, payment, {"card_auto_approve": True})


async def test_timer_respects_aware_offsets_and_future_timestamps(session) -> None:  # type: ignore[no-untyped-def]
    from datetime import timezone

    shop, customer, old = await pending_receipt(session)
    future = (await engine.start(session, customer, "card", 100_000)).payment
    await shop_settings.set_many(session, shop.id, {"card_auto_approve_after_minutes": 30})
    for payment in (old, future):
        await engine.attach_receipt(session, payment, file_id=f"receipt-{payment.id}", text=None)
    old.meta = {**old.meta, "receipt_at": (
        utcnow() - timedelta(hours=1)
    ).astimezone(timezone(timedelta(hours=3, minutes=30))).isoformat()}
    future.meta = {**future.meta, "receipt_at": (utcnow() + timedelta(hours=1)).isoformat()}
    await session.commit()
    assert await cards.approve_late(session) == 1
    await session.refresh(old)
    await session.refresh(future)
    assert old.status == PaymentStatus.PAID
    assert future.status == PaymentStatus.VERIFYING
