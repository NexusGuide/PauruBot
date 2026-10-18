"""Durable fail-closed creation for keyed hosted-invoice top-ups.

The private transaction owns only this payment. Never commit the request's session:
its unrelated writes must remain rollbackable. An ambiguous attempt is not retried.
Provider-specific reconciliation is a separate release gate, not an automatic POST.
"""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Payment, PaymentStatus
from paurubot.db.session import get_sessionmaker
from paurubot.payments import engine
from paurubot.payments.base import PaymentError
from paurubot.payments.intents import META_KEY, replay
from paurubot.payments.providers.invoices import invoice_cents

ATTEMPT_KEY = "hosted_attempt_v1"


async def start_hosted(
    caller: AsyncSession,
    customer: Customer,
    provider_name: str,
    amount: int,
    storage_key: str,
) -> engine.Started:
    """Persist an attempt before external I/O; only its creator may send creation once."""
    fingerprint: dict[str, str | int] = {
        "provider": provider_name,
        "amount": amount,
        "currency": "IRT",
    }
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": storage_key},
        )
        payment = await session.scalar(select(Payment).where(Payment.idempotency_key == storage_key))
        if payment is not None:
            result = replay(payment, customer, fingerprint)
            # The returned payment belongs to the caller, not the private session.
            owned = await caller.get(Payment, payment.id, populate_existing=True)
            if owned is None:
                raise PaymentError("payment_intent_closed")
            return engine.Started(owned, result.checkout)
        owner = await session.get(Customer, customer.id)
        if owner is None or owner.shop_id != customer.shop_id:
            # Uncommitted/new principals must be committed by their owning workflow first.
            raise PaymentError("payment_intent_conflict")
        payment, cfg = await engine._prepare(session, owner, provider_name, amount, storage_key=storage_key)
        # Freeze outgoing USD units before the durable pre-call commit, not after I/O.
        payment.provider_amount = await invoice_cents(payment, cfg)
        payment.provider_currency = "USD"
        payment.meta = {
            META_KEY: {"request": fingerprint},
            ATTEMPT_KEY: {"state": "uncertain", "started_at": utcnow().isoformat()},
        }
        payment_id = payment.id
        # After this commit, timeout/cancellation/process death cannot erase the attempt.
        await session.commit()

        # No retry wrapper: even PaymentError may mean the provider accepted the invoice.
        # A later call sees CREATED + no checkout and fails closed via replay().
        checkout = await engine.provider(provider_name).create(payment, cfg)
        # Expiration/review may have raced with the provider. Do not resurrect a closed row.
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
            META_KEY: {"request": fingerprint, "checkout": asdict(checkout)},
            ATTEMPT_KEY: {
                **(current.meta or {}).get(ATTEMPT_KEY, {}),
                "state": "ready",
            },
        }
        if current.status == PaymentStatus.CREATED:
            current.status = (
                PaymentStatus.EXPIRED
                if current.expires_at and current.expires_at <= utcnow()
                else PaymentStatus.PENDING
            )
        await session.commit()
        result = replay(current, owner, fingerprint)
        owned = await caller.get(Payment, payment_id, populate_existing=True)
        if owned is None:
            raise PaymentError("payment_intent_closed")
        return engine.Started(owned, result.checkout)
