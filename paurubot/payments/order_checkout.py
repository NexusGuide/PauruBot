"""One durable provider-creation attempt per committed Mini App order.

Only the private session is committed. Uncertain/closed/legacy attempts never
trigger another creation or a switch to another provider on the same order.
Bot/guest/unkeyed top-up workflows are deliberately not rerouted here.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderStatus, Payment, PaymentStatus
from paurubot.db.session import get_sessionmaker
from paurubot.payments import engine
from paurubot.payments.base import Checkout, PaymentError
from paurubot.payments.durable_checkout import ATTEMPT_KEY as HOSTED_ATTEMPT_KEY
from paurubot.payments.intents import META_KEY, replay
from paurubot.payments.providers.invoices import invoice_cents

ATTEMPT_KEY = "order_attempt_v1"


def _pending(order: Order) -> None:
    if order.status != OrderStatus.PENDING or (order.expires_at and order.expires_at <= utcnow()):
        raise PaymentError("order_expired")


def _fingerprint(order: Order, name: str) -> dict[str, str | int]:
    # Wallet balance may change while a customer retries. Replay the original
    # invoice, rather than computing a second price/payment from the new balance.
    return {"provider": name, "order_id": order.id, "order_amount": order.amount, "currency": "IRT"}


async def _order(session: AsyncSession, customer: Customer, order_id: int) -> Order:
    row = await session.scalar(
        select(Order)
        .where(Order.id == order_id, Order.shop_id == customer.shop_id, Order.customer_id == customer.id)
        .with_for_update(),
        execution_options={"populate_existing": True},
    )
    if row is None:
        raise PaymentError("order_expired")
    return row


async def _owned(caller: AsyncSession, payment_id: int, result: engine.Started) -> engine.Started:
    payment = await caller.get(Payment, payment_id, populate_existing=True)
    if payment is None:
        raise PaymentError("payment_intent_closed")
    return engine.Started(payment, result.checkout)


async def _create(name: str, payment: Payment, cfg: dict[str, Any]) -> Checkout:
    checkout = await engine.provider(name).create(payment, cfg)
    if name == "stars":
        from aiogram.types import LabeledPrice

        from paurubot.notifications.telegram import bot_for

        bot = await bot_for(payment.shop_id)
        if bot is None:
            raise PaymentError("gateway_error")
        async with asyncio.timeout(20):
            link = await bot.create_invoice_link(
                title="Order payment",
                description=f"Payment #{payment.id}",
                payload=str(checkout.instructions["payload"]),
                currency="XTR",
                prices=[LabeledPrice(label="Order payment", amount=int(checkout.instructions["stars"]))],
            )
        checkout.instructions = {**checkout.instructions, "invoice_link": link}
    return checkout


async def start(caller: AsyncSession, customer: Customer, order: Order, provider_name: str) -> engine.Started:
    """Called only for authenticated, already committed Mini App orders, not new guest drafts."""
    if order in caller.new or order in caller.deleted or caller.is_modified(order):
        raise PaymentError("payment_intent_conflict")
    maker = get_sessionmaker()
    async with maker() as session:
        current_order = await _order(session, customer, order.id)
        _pending(current_order)
        fingerprint = _fingerprint(current_order, provider_name)
        # Inspect *all* historical payments for the order, not just this provider
        # or a new key. Legacy/multiple snapshots cannot safely be guessed.
        existing = list(await session.scalars(select(Payment).where(Payment.order_id == order.id).limit(2)))
        if existing:
            payment = existing[0]
            if (
                len(existing) != 1
                or payment.idempotency_key != f"order-pay:v1:{customer.shop_id}:{customer.id}:{order.id}"
                or payment.order_id != order.id
            ):
                raise PaymentError("payment_intent_conflict")
            result = replay(payment, customer, fingerprint)
            return await _owned(caller, payment.id, result)
        owner = await session.get(Customer, customer.id)
        if owner is None or owner.shop_id != customer.shop_id or owner.is_banned:
            raise PaymentError("payment_intent_closed")
        balance = await engine.wallet_balance(session, owner.shop_id, owner.id)
        due = engine.amount_due(current_order, balance)
        if due <= 0:
            raise PaymentError("balance_enough_use_wallet")
        payment, cfg = await engine._prepare(
            session,
            owner,
            provider_name,
            due,
            order=current_order,
            storage_key=f"order-pay:v1:{owner.shop_id}:{owner.id}:{current_order.id}",
        )
        attempt: dict[str, Any] = {"state": "uncertain", "started_at": utcnow().isoformat()}
        payment.meta = {META_KEY: {"request": fingerprint}, ATTEMPT_KEY: attempt}
        if provider_name in {"nowpayments", "plisio"}:
            payment.provider_amount = await invoice_cents(payment, cfg)
            payment.provider_currency = "USD"
            payment.meta = {**payment.meta, HOSTED_ATTEMPT_KEY: dict(attempt)}
        if current_order.expires_at and payment.expires_at and current_order.expires_at < payment.expires_at:
            current_order.expires_at = payment.expires_at
        payment_id = payment.id
        # All participating methods serialize on the same order row. The durable
        # reservation survives caller rollback, task cancellation and process death.
        await session.commit()

        before_meta = dict(payment.meta or {})
        checkout = await _create(provider_name, payment, cfg)
        # CardProvider annotates its selected card; retain only provider-added
        # metadata while loading fresh status/evidence written by callbacks/staff.
        added_meta = {k: v for k, v in (payment.meta or {}).items() if k not in before_meta}
        with session.no_autoflush:
            current_order = await _order(session, customer, order.id)
            current = await session.scalar(
                select(Payment).where(Payment.id == payment_id).with_for_update(),
                execution_options={"populate_existing": True},
            )
        if current is None:
            raise PaymentError("payment_intent_closed")
        current.provider_ref = checkout.provider_ref
        current.provider_amount = checkout.provider_amount
        current.provider_currency = checkout.provider_currency
        current.meta = {
            **(current.meta or {}),
            **added_meta,
            META_KEY: {"request": fingerprint, "checkout": asdict(checkout)},
            ATTEMPT_KEY: {**attempt, "state": "ready"},
        }
        if HOSTED_ATTEMPT_KEY in before_meta:
            current.meta = {**current.meta, HOSTED_ATTEMPT_KEY: {**attempt, "state": "ready"}}
        if current.status == PaymentStatus.CREATED:
            current.status = (
                PaymentStatus.EXPIRED
                if current.expires_at and current.expires_at <= utcnow()
                else PaymentStatus.PENDING
            )
        # Preserve concurrent cancellation/expiry/payment decisions. Retain the
        # provider evidence even if the order can no longer be paid/provisioned.
        await session.commit()
        _pending(current_order)
        result = replay(current, owner, fingerprint)
        return await _owned(caller, payment_id, result)
