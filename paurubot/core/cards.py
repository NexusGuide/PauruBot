"""Card-to-card safety (PAY-17, PAY-18): customers the shop trusts or blocks, cards a customer
registers before paying, which card is shown, a random tail on the amount and late auto-approval."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.core.formatting import to_latin_digits
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus, VerifiedCard
from paurubot.security.tokens import sha256_hex

TRUST_LEVELS = ("normal", "trusted", "blocked")
CARD_PICKS = ("all", "random", "rotate")
TAIL_MAX = 999


class CardError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def digits(text: str) -> str:
    return "".join(ch for ch in to_latin_digits(text or "") if ch.isdigit())


def luhn_ok(number: str) -> bool:
    total = 0
    for index, ch in enumerate(reversed(number)):
        value = int(ch)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def masked(number: str) -> str:
    return f"{number[:4]}-{number[4:6]}**-****-{number[-4:]}"


def pick_accounts(accounts: list[dict[str, Any]], mode: str, payment_id: int) -> list[dict[str, Any]]:
    """All active cards, one at random, or one in turn (by payment)."""
    if len(accounts) <= 1 or mode not in {"random", "rotate"}:
        return accounts
    if mode == "random":
        return [secrets.choice(accounts)]
    return [accounts[payment_id % len(accounts)]]


def random_tail(amount: int, high: int) -> int:
    """A few Toman added so each receipt matches one payment; skipped when it would pass the cap."""
    tail = 1 + secrets.randbelow(TAIL_MAX)
    return amount + tail if amount + tail <= high else amount


# --- registered cards -----------------------------------------------------------------------


async def has_verified(session: AsyncSession, customer: Customer) -> bool:
    found = await session.scalar(
        select(VerifiedCard.id).where(
            VerifiedCard.shop_id == customer.shop_id,
            VerifiedCard.customer_id == customer.id,
            VerifiedCard.status == "approved",
        )
    )
    return found is not None


async def needs_verification(session: AsyncSession, customer: Customer, amount: int) -> bool:
    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg["card_verify_enabled"] or customer.card_trust == "trusted":
        return False
    if amount < int(cfg["card_verify_min_amount"] or 0):
        return False
    return not await has_verified(session, customer)


async def register(
    session: AsyncSession, customer: Customer, number_text: str, photo_file_id: str | None
) -> tuple[VerifiedCard, Customer | None]:
    """A card waiting for staff review, and another customer who already registered it, if any."""
    number = digits(number_text)
    if len(number) != 16 or not luhn_ok(number):
        raise CardError("card_number_invalid")
    # Shared with Telegram registration: serialize pending checks across entry points.
    await session.scalar(
        select(Customer)
        .where(Customer.id == customer.id, Customer.shop_id == customer.shop_id)
        .with_for_update()
    )
    pending = await session.scalar(
        select(VerifiedCard.id).where(
            VerifiedCard.shop_id == customer.shop_id,
            VerifiedCard.customer_id == customer.id,
            VerifiedCard.status == "pending",
        )
    )
    if pending is not None:
        raise CardError("card_verify_pending")
    digest = sha256_hex(f"{customer.shop_id}:{number}")
    other = await session.scalar(
        select(Customer)
        .join(VerifiedCard, VerifiedCard.customer_id == Customer.id)
        .where(
            VerifiedCard.shop_id == customer.shop_id,
            VerifiedCard.digest == digest,
            VerifiedCard.customer_id != customer.id,
        )
        .limit(1)
    )
    card = VerifiedCard(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        last4=number[-4:],
        masked=masked(number),
        digest=digest,
        photo_file_id=photo_file_id,
        status="pending",
    )
    session.add(card)
    await session.flush()
    return card, other


async def review(session: AsyncSession, card: VerifiedCard, approve: bool, staff_id: int | None) -> bool:
    if card.status != "pending":
        return False
    card.status = "approved" if approve else "rejected"
    card.reviewed_by_staff_id = staff_id
    card.reviewed_at = utcnow()
    await session.flush()
    return True


# --- receipts nobody looked at --------------------------------------------------------------


async def approve_late(session: AsyncSession) -> int:
    """Approve card receipts left unreviewed past the shop's limit (PAY-17), within its amount cap.

    Duplicates, risky and blocked customers always wait for a person.
    """
    from paurubot.payments import engine

    rows = list(
        await session.scalars(
            select(Payment).where(
                Payment.provider == "card",
                Payment.status == PaymentStatus.VERIFYING,
                Payment.receipt_file_id.is_not(None),
                Payment.duplicate_of_id.is_(None),
            )
        )
    )
    approved = 0
    now = utcnow()
    for payment in rows:
        cfg = await shop_settings.load(session, payment.shop_id)
        minutes = int(cfg["card_auto_approve_after_minutes"] or 0)
        if minutes <= 0:
            continue
        sent = (payment.meta or {}).get("receipt_at")
        if not sent or datetime.fromisoformat(str(sent)) > now - timedelta(minutes=minutes):
            continue
        cap = int(cfg["card_auto_max_amount"] or 0)
        if cap and payment.amount > cap:
            continue
        customer = await session.get(Customer, payment.customer_id)
        if customer is None or customer.risk in {"medium", "high"} or customer.card_trust == "blocked":
            continue
        payment.review_note = f"auto-approved after {minutes} minutes"
        if await engine.mark_paid(session, payment.id, provider_tx_id=f"card:{payment.id}"):
            approved += 1
    await session.commit()
    return approved
