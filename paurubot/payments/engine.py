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

from sqlalchemy import ColumnElement, and_, func, or_, select, update
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
from paurubot.payments.providers.gateways import GATEWAYS
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
    **GATEWAYS,
}
OPEN_STATUSES = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.VERIFYING)
LEASE_SECONDS = 120
# How long after expiry a gateway's confirmation still credits the payment, and polling goes on.
LATE_CREDIT = timedelta(days=2)


def provider(name: str) -> PaymentProvider:
    try:
        return PROVIDERS[name]
    except KeyError as exc:
        raise PaymentError("method_disabled") from exc


async def available(session: AsyncSession, shop_id: int, customer: Customer | None = None) -> list[str]:
    cfg = await shop_settings.load(session, shop_id)
    names = [name for name, prov in PROVIDERS.items() if await prov.enabled(cfg)]
    if customer is not None and customer.card_trust == "blocked":
        names = [name for name in names if name != "card"]  # PAY-17
    return names


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
    if prov.name == "card":
        from paurubot.core import cards

        if customer.card_trust == "blocked":
            raise PaymentError("card_blocked")
        if await cards.needs_verification(session, customer, amount):
            raise PaymentError("card_verify_required")
        if cfg["card_random_tail"]:
            amount = cards.random_tail(amount, high)
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
    payment.meta = {**(payment.meta or {}), "receipt_at": utcnow().isoformat()}
    await session.flush()
    cfg = await shop_settings.load(session, payment.shop_id)
    if duplicate is not None and duplicate.receipt_sha256 == payment.receipt_sha256:
        if cfg["receipt_duplicate_block"]:
            await reject(session, payment.id, staff_id=None, note="duplicate receipt")
        return duplicate
    fixing = bool((payment.meta or {}).get("fix_requests"))  # staff asked for this one (F-23)
    if duplicate is None and not fixing and await auto_approvable(session, payment, cfg):
        payment.review_note = "auto-approved"
        await mark_paid(session, payment.id, provider_tx_id=f"card:{payment.id}")
    return duplicate


async def auto_approvable(session: AsyncSession, payment: Payment, cfg: dict[str, Any]) -> bool:
    """Card auto-approve rules (PAY-05): small amounts from customers with a clean card history."""
    if payment.receipt_file_id is None:
        return False
    customer = await session.get(Customer, payment.customer_id)
    if customer is not None and customer.card_trust == "trusted":
        return True  # the shop vouched for this customer (PAY-17)
    if not cfg.get("card_auto_approve"):
        return False
    if payment.amount > int(cfg.get("card_auto_max_amount") or 0):
        return False
    if customer is not None and customer.risk in {"medium", "high"}:
        return False  # risky customers always get a human look (BOT-08)
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
        meta = {**(payment.meta or {}), "disputed_tx": provider_tx_id, "disputed_amount": int(amount)}
        await session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(OPEN_STATUSES))
            .values(status=PaymentStatus.DISPUTED, failure_reason="amount mismatch", meta=meta)
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


async def approve_card(session: AsyncSession, payment_id: int, staff_id: int | None) -> bool:
    return await mark_paid(session, payment_id, provider_tx_id=f"card:{payment_id}", reviewer_id=staff_id)


def disputed_credit(payment: Payment) -> int | None:
    """What a gateway said really arrived for a payment parked over its amount, if anything."""
    meta = payment.meta or {}
    if payment.status != PaymentStatus.DISPUTED or not meta.get("disputed_tx"):
        return None
    amount = int(meta.get("disputed_amount") or 0)
    return amount if amount > 0 else None


def manually_reviewable(payment: Payment) -> bool:
    """Card receipts, crypto transfers that arrived after the payment expired, and gateway
    payments where a different amount arrived."""
    return (
        payment.provider == "card"
        or bool((payment.meta or {}).get("late_tx"))
        or disputed_credit(payment) is not None
    )


def review_filter() -> ColumnElement[bool]:
    """SQL side of awaiting_review(), so a review queue is never crowded out by other rows."""
    return or_(
        and_(
            Payment.status == PaymentStatus.VERIFYING,
            or_(Payment.provider == "card", Payment.meta["late_tx"].as_string().is_not(None)),
        ),
        and_(Payment.status == PaymentStatus.DISPUTED, Payment.meta["disputed_tx"].as_string().is_not(None)),
    )


def awaiting_review(payment: Payment) -> bool:
    if disputed_credit(payment) is not None:
        return True
    return payment.status == PaymentStatus.VERIFYING and manually_reviewable(payment)


async def approve_manual(session: AsyncSession, payment_id: int, staff_id: int | None) -> bool:
    payment = await session.get(Payment, payment_id)
    if payment is None or not manually_reviewable(payment):
        return False
    if payment.provider == "card":
        return await approve_card(session, payment_id, staff_id)
    credit = disputed_credit(payment)
    if credit is not None:
        # Staff accept what the gateway reported; the payment is credited at that amount.
        reopened = await session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == PaymentStatus.DISPUTED)
            .values(status=PaymentStatus.VERIFYING, amount=credit, failure_reason=None)
        )
        if not affected(reopened):
            return False
        await session.refresh(payment)
        tx = str(payment.meta["disputed_tx"])
        return await mark_paid(session, payment_id, provider_tx_id=tx, reviewer_id=staff_id)
    tx = str(payment.meta["late_tx"])
    return await mark_paid(session, payment_id, provider_tx_id=tx, reviewer_id=staff_id)


async def reject(session: AsyncSession, payment_id: int, *, staff_id: int | None, note: str = "") -> bool:
    result = await session.execute(
        update(Payment)
        .where(Payment.id == payment_id, Payment.status.in_([*OPEN_STATUSES, PaymentStatus.DISPUTED]))
        .values(
            status=PaymentStatus.FAILED,
            reviewed_by_staff_id=staff_id,
            reviewed_at=utcnow(),
            review_note=note[:255] or None,
            failure_reason="rejected",
        )
    )
    return bool(affected(result))


FIX_KINDS = ("amount", "card")
MAX_FIX_HISTORY = 10


def fix_count(payment: Payment) -> int:
    """How many times staff sent this payment's receipt back for a fix (F-23)."""
    return len((payment.meta or {}).get("fix_requests") or [])


async def request_fix(
    session: AsyncSession, payment_id: int, *, staff_id: int | None, kind: str, note: str = ""
) -> bool:
    """Send a card receipt back to the customer instead of rejecting it (F-23).

    The payment goes back to waiting for a receipt (PENDING) with a fresh deadline; nothing is
    credited or refunded and the provider is untouched. The old receipt is kept in `meta`.
    False when the payment is not a card receipt under review anymore.
    """
    if kind not in FIX_KINDS:
        raise ValueError(f"unknown fix kind {kind!r}")
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.provider != "card" or payment.status != PaymentStatus.VERIFYING:
        return False
    cfg = await shop_settings.load(session, payment.shop_id)
    now = utcnow()
    meta = dict(payment.meta or {})
    history = list(meta.get("fix_requests") or [])
    history.append(
        {
            "kind": kind,
            "note": note[:255] or None,
            "by": staff_id,
            "at": now.isoformat(),
            "receipt_file_id": payment.receipt_file_id,
            "receipt_text": payment.receipt_text,
        }
    )
    meta["fix_requests"] = history[-MAX_FIX_HISTORY:]
    meta.pop("receipt_at", None)
    expires = now + timedelta(minutes=int(cfg["payment_expire_minutes"] or 60))
    result = await session.execute(
        update(Payment)
        .where(
            Payment.id == payment_id,
            Payment.provider == "card",
            Payment.status == PaymentStatus.VERIFYING,
        )
        .values(
            status=PaymentStatus.PENDING,
            receipt_file_id=None,
            receipt_text=None,
            duplicate_of_id=None,
            expires_at=expires,
            meta=meta,
        )
    )
    if not affected(result):
        return False
    await session.refresh(payment)
    if payment.order_id:
        order = await session.get(Order, payment.order_id)
        if (
            order is not None
            and order.status == OrderStatus.PENDING
            and order.expires_at
            and order.expires_at < expires
        ):
            order.expires_at = expires  # keep the order alive while the customer fixes the receipt
    await session.flush()
    return True


async def verify(session: AsyncSession, payment_id: int, params: dict[str, Any]) -> bool:
    """Server-to-server verification for gateway callbacks and Telegram payments."""
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return False
    prov = provider(payment.provider)
    late = (
        payment.status == PaymentStatus.EXPIRED
        and getattr(prov, "late_ok", False)
        and payment.expires_at is not None
        and payment.expires_at > utcnow() - LATE_CREDIT
    )
    if payment.status not in OPEN_STATUSES and not late:
        return payment.status == PaymentStatus.PAID
    cfg = await shop_settings.load(session, payment.shop_id)
    result = await prov.verify(payment, cfg, params)
    if result.paid and result.provider_tx_id and late:
        # The gateway confirms the money arrived after our deadline: still the customer's money.
        reopened = await session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == PaymentStatus.EXPIRED)
            .values(status=PaymentStatus.VERIFYING, review_note="paid after expiry")
        )
        if not affected(reopened):
            return False
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


async def pollable(session: AsyncSession, limit: int = 100) -> list[int]:
    """Open (and recently expired) payments of gateways that are settled by asking them."""
    names = [name for name, prov in PROVIDERS.items() if getattr(prov, "polls", False)]
    now = utcnow()
    rows = await session.scalars(
        select(Payment.id)
        .where(
            Payment.provider.in_(names),
            Payment.provider_ref.is_not(None),
            or_(
                Payment.status == PaymentStatus.PENDING,
                (Payment.status == PaymentStatus.EXPIRED) & (Payment.expires_at > now - LATE_CREDIT),
            ),
            Payment.created_at < now - timedelta(seconds=20),
        )
        .order_by(func.random())  # a backlog larger than one pass can't starve newer payments
        .limit(limit)
    )
    return list(rows)


async def poll_gateways(session: AsyncSession) -> int:
    """Ask polling gateways about each open payment; returns how many were confirmed. Commits."""
    confirmed = 0
    for payment_id in await pollable(session):
        try:
            ok = await verify(session, payment_id, {"source": "poll"})
        except PaymentError:
            ok = False
        await session.commit()
        if ok:
            confirmed += 1
            await fulfill_one(session, payment_id)
    return confirmed


def amount_to_pay(payment: Payment) -> int:
    """What the customer pays at the gateway in Toman, including any gateway fee or markup."""
    if payment.provider_currency == "IRT" and payment.provider_amount:
        return int(payment.provider_amount)
    if payment.provider_currency == "IRR" and payment.provider_amount:
        return int(payment.provider_amount) // 10
    return int(payment.amount)


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
