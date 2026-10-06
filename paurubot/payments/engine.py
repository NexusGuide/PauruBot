"""Payment lifecycle and lease-based fulfillment (PAY-01, PAY-02, PAY-15, PAY-16).

Money only moves in `fulfill_one`: one transaction credits the wallet, settles the order
(charge + provisioning job) and marks the payment fulfilled. A crashed worker leaves an expired
lease that the next run picks up; idempotency keys make a second pass a no-op.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import affected, utcnow
from paurubot.db.models import Customer, Order, OrderStatus, Payment, PaymentPurpose, PaymentStatus
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.payments import receipts
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider
from paurubot.payments.bonus import bonus_for
from paurubot.payments.providers.card import CardProvider
from paurubot.payments.providers.crypto import PROVIDERS as CRYPTO_PROVIDERS
from paurubot.payments.providers.invoices import NowPaymentsProvider, PlisioProvider
from paurubot.payments.providers.stars import StarsProvider
from paurubot.payments.providers.zarinpal import ZarinpalProvider
from paurubot.security.tokens import new_token
from paurubot.worker import queue

log = logging.getLogger(__name__)

PROVIDERS: dict[str, PaymentProvider] = {
    "card": CardProvider(),
    "zarinpal": ZarinpalProvider(),
    "stars": StarsProvider(),
    **CRYPTO_PROVIDERS,
    "nowpayments": NowPaymentsProvider(),
    "plisio": PlisioProvider(),
}
OPEN_STATUSES = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.VERIFYING)
LEASE_SECONDS = 120


def provider(name: str) -> PaymentProvider:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise PaymentError("method_disabled") from exc


async def available(session: AsyncSession, shop_id: int) -> list[str]:
    cfg = await shop_settings.load(session, shop_id)
    return [name for name, prov in PROVIDERS.items() if await prov.enabled(cfg)]


async def topup_bounds(session: AsyncSession, customer: Customer) -> tuple[int, int]:
    cfg = await shop_settings.load(session, customer.shop_id)
    low = int(cfg["min_topup"] or 0)
    profile = customer.reseller_profile
    if profile is not None and profile.is_current and profile.group is not None:
        low = max(low, int(profile.group.min_topup or 0))
    return low, int(cfg["max_topup"] or 10**12)


def amount_due(order: Order, balance: int) -> int:
    """What an order still needs on top of the wallet, rounded up to a whole thousand."""
    due = int(order.amount) - int(balance)
    if due <= 0:
        return 0
    return -(-due // 1000) * 1000


@dataclass(slots=True)
class Started:
    payment: Payment
    checkout: Checkout


async def start(
    session: AsyncSession,
    customer: Customer,
    provider_name: str,
    amount: int,
    *,
    order: Order | None = None,
) -> Started:
    """Create a payment and ask the provider how the customer should pay it."""
    cfg = await shop_settings.load(session, customer.shop_id)
    prov = provider(provider_name)
    if not await prov.enabled(cfg):
        raise PaymentError("method_disabled")
    low, high = await topup_bounds(session, customer)
    if amount <= 0 or amount > high or (order is None and amount < low):
        raise PaymentError("amount_out_of_range", low=low, high=high)
    if order is not None and (order.customer_id != customer.id or order.status != OrderStatus.PENDING):
        raise PaymentError("order_expired")
    minutes = int(cfg["payment_expire_minutes"] or 60)
    payment = Payment(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        order_id=order.id if order else None,
        purpose=PaymentPurpose.ORDER if order else PaymentPurpose.TOPUP,
        provider=prov.name,
        status=PaymentStatus.CREATED,
        amount=amount,
        idempotency_key=f"{prov.name}:{uuid.uuid4().hex}",
        expires_at=utcnow() + timedelta(minutes=minutes),
    )
    session.add(payment)
    await session.flush()
    checkout = await prov.create(payment, cfg)
    payment.provider_ref = checkout.provider_ref
    payment.provider_amount = checkout.provider_amount
    payment.provider_currency = checkout.provider_currency
    payment.status = PaymentStatus.PENDING
    if (
        order is not None
        and order.expires_at
        and payment.expires_at
        and order.expires_at < payment.expires_at
    ):
        order.expires_at = payment.expires_at  # keep the order alive while the payment is open
    await session.flush()
    return Started(payment, checkout)


async def attach_receipt(
    session: AsyncSession,
    payment: Payment,
    *,
    file_id: str | None,
    text: str | None,
    image: bytes | None = None,
) -> Payment | None:
    """Store a card receipt and return an earlier payment it duplicates, if any."""
    if payment.provider != "card" or payment.status != PaymentStatus.PENDING or payment.receipt_file_id:
        raise PaymentError("payment_not_waiting")
    payment.receipt_file_id = file_id
    payment.receipt_text = (text or "")[:1000] or None
    duplicate = None
    if image:
        payment.receipt_sha256 = receipts.sha256(image)
        payment.receipt_phash = receipts.dhash(image)
        duplicate = await receipts.find_duplicate(
            session, payment.shop_id, payment.id, payment.receipt_sha256, payment.receipt_phash
        )
        payment.duplicate_of_id = duplicate.id if duplicate else None
    payment.status = PaymentStatus.VERIFYING
    await session.flush()
    cfg = await shop_settings.load(session, payment.shop_id)
    if duplicate is not None and duplicate.receipt_sha256 == payment.receipt_sha256:
        if cfg["receipt_duplicate_block"]:
            await reject(session, payment.id, staff_id=None, note="duplicate receipt")
        return duplicate
    if duplicate is None and await auto_approvable(session, payment, cfg):
        payment.review_note = "auto-approved"
        await mark_paid(session, payment.id, provider_tx_id=f"card:{payment.id}")
    return duplicate


async def auto_approvable(session: AsyncSession, payment: Payment, cfg: dict[str, Any]) -> bool:
    """Card auto-approve rules (PAY-05): small amounts from customers with a clean card history."""
    if not cfg.get("card_auto_approve") or payment.receipt_file_id is None:
        return False
    if payment.amount > int(cfg.get("card_auto_max_amount") or 0):
        return False
    history = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.customer_id == payment.customer_id,
            Payment.provider == "card",
            Payment.status == PaymentStatus.PAID,
            Payment.reviewed_by_staff_id.is_not(None),
        )
    )
    rejected = await session.scalar(
        select(func.count(Payment.id)).where(
            Payment.customer_id == payment.customer_id,
            Payment.provider == "card",
            Payment.status.in_([PaymentStatus.FAILED, PaymentStatus.DISPUTED]),
            Payment.reviewed_at.is_not(None),
        )
    )
    return int(history or 0) >= int(cfg.get("card_auto_min_history") or 0) and not rejected


async def mark_paid(
    session: AsyncSession,
    payment_id: int,
    *,
    provider_tx_id: str,
    amount: int | None = None,
    reviewer_id: int | None = None,
) -> bool:
    """Compare-and-set to PAID. False when the payment was already settled or closed."""
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return False
    if amount is not None and amount != payment.amount:
        log.warning("payment %s amount mismatch", payment_id)
        await session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
            .values(status=PaymentStatus.DISPUTED, failure_reason="amount mismatch")
        )
        return False
    try:
        async with session.begin_nested():
            result = await session.execute(
                update(Payment)
                .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
                .values(
                    status=PaymentStatus.PAID,
                    provider_tx_id=provider_tx_id,
                    paid_at=utcnow(),
                    reviewed_by_staff_id=reviewer_id,
                    reviewed_at=utcnow() if reviewer_id else None,
                )
            )
    except IntegrityError:
        # Same provider transaction already credited another payment: a replay.
        await session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
            .values(status=PaymentStatus.DISPUTED, failure_reason="provider transaction reused")
        )
        return False
    if not affected(result):
        return False
    await queue.enqueue(
        session,
        "payments",
        "fulfill",
        {"payment_id": payment_id},
        shop_id=payment.shop_id,
        dedupe_key=f"payment:{payment_id}:fulfill",
    )
    return True


async def approve_card(session: AsyncSession, payment_id: int, staff_id: int) -> bool:
    return await mark_paid(session, payment_id, provider_tx_id=f"card:{payment_id}", reviewer_id=staff_id)


def manually_reviewable(payment: Payment) -> bool:
    """Card receipts, and crypto transfers that arrived after the payment expired."""
    return payment.provider == "card" or bool((payment.meta or {}).get("late_tx"))


async def approve_manual(session: AsyncSession, payment_id: int, staff_id: int) -> bool:
    payment = await session.get(Payment, payment_id)
    if payment is None or not manually_reviewable(payment):
        return False
    if payment.provider == "card":
        return await approve_card(session, payment_id, staff_id)
    tx = str(payment.meta["late_tx"])
    return await mark_paid(session, payment_id, provider_tx_id=tx, reviewer_id=staff_id)


async def reject(session: AsyncSession, payment_id: int, *, staff_id: int | None, note: str = "") -> bool:
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
        .values(
            status=PaymentStatus.FAILED,
            reviewed_by_staff_id=staff_id,
            reviewed_at=utcnow(),
            review_note=note[:255] or None,
            failure_reason="rejected",
        )
    )
    return bool(affected(result))


async def verify(session: AsyncSession, payment_id: int, params: dict[str, Any]) -> bool:
    """Server-to-server verification for gateway callbacks and Telegram payments."""
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.status not in OPEN_STATUSES:
        return bool(payment and payment.status == PaymentStatus.PAID)
    cfg = await shop_settings.load(session, payment.shop_id)
    result = await provider(payment.provider).verify(payment, cfg, params)
    if not result.paid or not result.provider_tx_id:
        if "canceled" in result.detail:
            await session.execute(
                update(Payment)
                .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
                .values(status=PaymentStatus.FAILED, failure_reason=result.detail[:255])
            )
        return False
    return await mark_paid(session, payment_id, provider_tx_id=result.provider_tx_id, amount=result.amount)


@dataclass(slots=True)
class Fulfilled:
    payment: Payment
    order_settled: bool
    order_error: str | None = None


async def fulfill_one(session: AsyncSession, payment_id: int) -> Fulfilled | None:
    """Credit a paid payment and settle its order under a lease. Commits."""
    token = new_token(16)
    now = utcnow()
    claimed = await session.execute(
        update(Payment)
        .where(
            Payment.id == payment_id,
            Payment.status == PaymentStatus.PAID,
            Payment.fulfilled_at.is_(None),
            or_(Payment.lease_until.is_(None), Payment.lease_until < now),
        )
        .values(
            claim_token=token,
            lease_until=now + timedelta(seconds=LEASE_SECONDS),
            attempts=Payment.attempts + 1,
        )
    )
    if not affected(claimed):
        return None
    await session.commit()

    payment = await session.get(Payment, payment_id, populate_existing=True)
    assert payment is not None
    await wallet.topup(
        session,
        payment.shop_id,
        payment.customer_id,
        payment.amount,
        provider=payment.provider,
        payment_id=payment.id,
    )
    bonus = 0
    if payment.purpose == PaymentPurpose.TOPUP:
        cfg = await shop_settings.load(session, payment.shop_id)
        bonus = bonus_for(cfg.get("topup_bonus_rules"), payment.provider, payment.amount)
        if bonus > 0:
            await wallet.reward(
                session,
                payment.shop_id,
                payment.customer_id,
                bonus,
                key=f"payment:{payment.id}:bonus",
                memo=f"bonus for payment #{payment.id}",
            )
    settled, error = False, None
    if payment.order_id:
        try:
            async with session.begin_nested():
                await orders.settle_from_wallet(session, payment.order_id)
            settled = True
        except orders.OrderError as exc:
            error = exc.key  # money stays in the wallet; the customer can reorder
    done = await session.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.claim_token == token)
        .values(fulfilled_at=utcnow(), lease_until=None)
    )
    if not affected(done):
        await session.rollback()  # lease was stolen; the other worker finishes the job
        return None
    await queue.enqueue(
        session,
        "notify",
        "payment_fulfilled",
        {"payment_id": payment.id, "order_settled": settled, "order_error": error, "bonus": bonus},
        shop_id=payment.shop_id,
        dedupe_key=f"payment:{payment.id}:notify",
    )
    await session.commit()
    return Fulfilled(payment, settled, error)


async def pending_fulfillment(session: AsyncSession, limit: int = 50) -> list[int]:
    now = utcnow()
    rows = await session.scalars(
        select(Payment.id)
        .where(
            Payment.status == PaymentStatus.PAID,
            Payment.fulfilled_at.is_(None),
            or_(Payment.lease_until.is_(None), Payment.lease_until < now),
        )
        .order_by(Payment.id)
        .limit(limit)
    )
    return list(rows)


async def expire_stale(session: AsyncSession) -> int:
    """Open payments past their deadline are closed; card receipts already sent stay for review."""
    result = await session.execute(
        update(Payment)
        .where(
            Payment.status.in_([PaymentStatus.CREATED, PaymentStatus.PENDING]),
            Payment.expires_at < utcnow(),
        )
        .values(status=PaymentStatus.EXPIRED)
    )
    return affected(result)


async def pay_order(session: AsyncSession, customer: Customer, order: Order, provider_name: str) -> Started:
    """Direct pay (PAY-15): pay only what the wallet is missing for this order."""
    balance = await wallet_balance(session, customer.shop_id, customer.id)
    due = amount_due(order, balance)
    if due <= 0:
        raise PaymentError("balance_enough_use_wallet")
    return await start(session, customer, provider_name, due, order=order)
