"""Void a payment a person approved by mistake (REF-01).

A card receipt, or an on-chain transfer staff accepted by hand, is approved by a person and can be
wrong: a fake or edited receipt, the wrong amount, a duplicate. Voiding takes back exactly what
that approval gave, and nothing else:

* the purchase the payment settled is refunded to the wallet, then the payment's own wallet credit
  (and its top-up bonus) is reversed, so the customer ends where they were before paying;
* the service that purchase created is turned off on its panel;
* the reseller's wholesale charge on that sale is returned, and the referral reward and level-1
  commission it paid are taken back while the receiving wallet still holds them. What cannot be
  taken back is reported to staff instead of blocking the void.

All money moves in the caller's transaction under row locks and ledger idempotency keys, so a second
click or a retry cannot move it twice. When the customer has already spent the credit elsewhere the
void is refused as a whole: a half-voided payment would be worse than none. The panel is changed
afterwards by a durable job under the service lease (SEC-F09), so a panel outage delays the stop
but never undoes the void.

Only payments a person reviewed can be voided. A gateway verified server to server cannot be fake
in this sense; neither can a transfer the bot matched on chain by itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerEntry,
    LedgerJournal,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentStatus,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.ledger.posting import InsufficientFunds, reverse
from paurubot.payments import engine
from paurubot.worker import queue

REASONS = ("fake_receipt", "wrong_amount", "duplicate", "mistake", "other")
NOTE_MAX = 200
# Orders whose money is held by the order itself: voiding refunds them first. Of those, only a
# purchase can be undone by turning off what it created; a renewal or an extra changed a service the
# customer had before, and taking that back needs a person.
UNDOABLE_KINDS = frozenset({OrderKind.PURCHASE})
IN_FLIGHT = frozenset({OrderStatus.PAID, OrderStatus.PROVISIONING})
SERVICE_JOB = "void_service"


class VoidError(Exception):
    def __init__(self, key: str, **params: Any) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(slots=True)
class Voided:
    payment: Payment
    order: Order | None
    service_id: int | None
    refunded_order: bool
    # Side payments of the sale that could not be taken back ("referral", "commission").
    kept: list[str] = field(default_factory=list)


def voidable(payment: Payment) -> bool:
    """True when staff may void this payment (the void re-checks everything under locks)."""
    return (
        payment.status == PaymentStatus.PAID
        and payment.fulfilled_at is not None
        and engine.manually_reviewable(payment)
    )


async def _journal(session: AsyncSession, key: str) -> LedgerJournal | None:
    return await session.scalar(select(LedgerJournal).where(LedgerJournal.idempotency_key == key))


async def _credited(session: AsyncSession, key: str) -> int:
    """What the journal under `key` paid into a wallet, 0 when there is none."""
    total = await session.scalar(
        select(func.coalesce(func.sum(LedgerEntry.amount), 0))
        .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
        .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
        .where(
            LedgerJournal.idempotency_key == key,
            LedgerEntry.amount > 0,
            LedgerAccount.customer_id.is_not(None),
        )
    )
    return int(total or 0)


async def _take_back(session: AsyncSession, key: str, *, as_key: str, actor: str, memo: str) -> bool | None:
    """Reverse the journal posted under `key`. None when there is none; False when the wallet
    that received it no longer holds the money. Never leaves a partial posting behind."""
    journal = await _journal(session, key)
    if journal is None:
        return None
    try:
        await reverse(session, journal.id, idempotency_key=as_key, memo=memo, actor=actor)
    except InsufficientFunds:
        return False
    return True


async def _charged(session: AsyncSession, order: Order) -> bool:
    """The order holds the customer's money: charged and not refunded since."""
    return (
        await _journal(session, f"order:{order.id}:charge") is not None
        and await _journal(session, f"order:{order.id}:refund") is None
    )


async def _undo_sale(
    session: AsyncSession, payment: Payment, order: Order, *, actor: str, memo: str
) -> tuple[Service | None, list[str]]:
    """Refund a delivered purchase and take back what the sale paid to others."""
    from paurubot.core import resale

    if order.status in IN_FLIGHT:
        raise VoidError("void_order_in_progress", order=order.id)
    if order.kind not in UNDOABLE_KINDS:
        raise VoidError("void_order_kind", order=order.id)
    params = dict(order.params or {})
    if params.get("stock_item_id") or params.get("stock_plan_id"):
        raise VoidError("void_stock_delivered", order=order.id)
    service = None
    refund = int(order.amount)
    if order.status == OrderStatus.COMPLETED:
        service = await session.get(Service, order.service_id) if order.service_id else None
        if service is None or service.customer_id != order.customer_id:
            raise VoidError("void_service_missing", order=order.id)
        await session.refresh(service, with_for_update=True)
        later = await session.scalar(
            select(Order.id)
            .where(
                Order.service_id == service.id,
                Order.id != order.id,
                Order.amount > 0,
                Order.status.in_([OrderStatus.COMPLETED, *IN_FLIGHT]),
            )
            .limit(1)
        )
        if later is not None:
            # A paid renewal or extra sits on this service: turning it off would take away what
            # that payment bought too. A person has to split it.
            raise VoidError("void_service_changed", order=order.id, later=later)
        # The customer deleted the service and got part of this purchase back already.
        refund = max(0, refund - await _credited(session, f"service:{service.id}:cancel"))
    elif order.status != OrderStatus.FAILED:
        raise VoidError("void_order_state", order=order.id, status=order.status.value)

    await wallet.refund(session, order.shop_id, order.customer_id, refund, order_id=order.id, actor=actor)
    await orders.release_holds(session, order)
    await resale.refund_wholesale(session, order)

    kept: list[str] = []
    referral = params.get("referral") or {}
    if referral.get("key"):
        taken = await _take_back(
            session, str(referral["key"]), as_key=f"order:{order.id}:void-referral", actor=actor, memo=memo
        )
        if taken is False:
            kept.append("referral")
    if referral.get("first") and "referral" not in kept:
        # The referred customer's first purchase did not happen after all.
        customer = await session.get(Customer, order.customer_id)
        if customer is not None:
            customer.referral_rewarded = False
    taken = await _take_back(
        session,
        f"order:{order.id}:commission",
        as_key=f"order:{order.id}:void-commission",
        actor=actor,
        memo=memo,
    )
    if taken is False:
        kept.append("commission")

    order.status = OrderStatus.REFUNDED
    order.error = f"voided: payment #{payment.id}"
    order.params = {**(order.params or {}), "voided_payment": payment.id}
    if service is not None:
        service.auto_renew = None  # never renew a service its payment did not pay for
    return service, kept


async def void(
    session: AsyncSession,
    payment_id: int,
    *,
    shop_id: int,
    actor: Actor,
    reason: str,
    note: str = "",
    block_card: bool = False,
) -> Voided:
    """Void one approved payment of `shop_id`. Changes nothing when it raises; the caller commits."""
    if reason not in REASONS:
        raise VoidError("void_reason")
    note = " ".join((note or "").split())[:NOTE_MAX]
    who = f"{actor.kind}:{actor.id}" if actor.id is not None else actor.kind
    async with session.begin_nested():
        payment = await session.scalar(
            select(Payment)
            .where(Payment.id == payment_id, Payment.shop_id == shop_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payment is None:
            raise VoidError("void_not_found")
        if payment.status == PaymentStatus.VOIDED:
            raise VoidError("void_already")
        if payment.status != PaymentStatus.PAID or not engine.manually_reviewable(payment):
            raise VoidError("void_not_voidable")
        if payment.fulfilled_at is None:
            raise VoidError("void_not_settled")  # the credit job is still running; try again shortly
        order = None
        if payment.order_id is not None:
            order = await session.scalar(
                select(Order)
                .where(Order.id == payment.order_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        memo = f"void of payment #{payment.id}"
        balance = await wallet_balance(session, payment.shop_id, payment.customer_id)
        service: Service | None = None
        kept: list[str] = []
        refunded = False
        if order is not None and order.shop_id == shop_id and await _charged(session, order):
            service, kept = await _undo_sale(session, payment, order, actor=who, memo=memo)
            refunded = True
        # The payment's own credit: what the customer holds must cover it, or nothing is voided.
        for key, as_key in (
            (f"payment:{payment.id}:credit", f"payment:{payment.id}:void"),
            (f"payment:{payment.id}:bonus", f"payment:{payment.id}:void-bonus"),
        ):
            if await _take_back(session, key, as_key=as_key, actor=who, memo=memo) is False:
                raise VoidError("void_wallet_spent", balance=balance, amount=payment.amount)

        customer = await session.get(Customer, payment.customer_id)
        if customer is not None:
            if block_card:
                customer.card_trust = "blocked"
            if refunded:
                await _recount_tier(session, customer)
        payment.status = PaymentStatus.VOIDED
        payment.review_note = f"voided: {reason}"[:255]
        payment.meta = {
            **(payment.meta or {}),
            "void": {
                "at": utcnow().isoformat(),
                "by": who,
                "reason": reason,
                "note": note or None,
                "order_id": order.id if refunded and order is not None else None,
                "service_id": service.id if service is not None else None,
                "kept": kept,
                "block_card": block_card,
            },
        }
        await session.flush()
    if service is not None and service.status != ServiceStatus.DELETED:
        await queue.enqueue(
            session,
            orders.PROVISION_QUEUE,
            SERVICE_JOB,
            {"service_id": service.id, "payment_id": payment.id},
            shop_id=shop_id,
            dedupe_key=f"payment:{payment.id}:void-service",
            max_attempts=10,
        )
    await queue.enqueue(
        session,
        "notify",
        "payment_voided",
        {"payment_id": payment.id},
        shop_id=shop_id,
        dedupe_key=f"payment:{payment.id}:voided",
    )
    await record(
        session,
        shop_id=shop_id,
        actor=actor,
        action="payment.voided",
        entity="payment",
        entity_id=payment.id,
        after={
            "reason": reason,
            "note": note or None,
            "order_id": order.id if refunded and order is not None else None,
            "service_id": service.id if service is not None else None,
            "kept": kept,
            "block_card": block_card,
        },
    )
    return Voided(payment, order if refunded else None, service.id if service else None, refunded, kept)


async def _recount_tier(session: AsyncSession, customer: Customer) -> None:
    """A voided purchase no longer counts toward the loyalty tier (it may go down, silently)."""
    from paurubot.core import growth

    cfg = await shop_settings.load(session, customer.shop_id)
    if (cfg.get("loyalty_enabled") or customer.tier_percent) and not customer.is_reseller:
        await growth.refresh_tier(session, customer, cfg)
