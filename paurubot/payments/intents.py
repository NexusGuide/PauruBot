"""Transactional replay protection for ordinary top-up checkout requests.

Hosted NowPayments/Plisio attempts are committed before invoice creation. Other
providers retain the transaction-scoped contract; external exactly-once is not claimed.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments.base import Checkout, PaymentError

KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}\Z")
META_KEY = "checkout_intent_v1"


async def start_once(
    session: AsyncSession,
    customer: Customer,
    provider_name: str,
    amount: int,
    key: str,
    *,
    order: Order | None = None,
) -> engine.Started:
    if not KEY.fullmatch(key):
        raise PaymentError("payment_intent_invalid")
    # Do not imply order/provisioning exactly-once semantics in this top-up change.
    if order is not None:
        raise PaymentError("payment_intent_invalid")
    digest = hashlib.sha256(f"{customer.shop_id}:{customer.id}:{key}".encode()).hexdigest()
    storage_key = f"topup:v1:{digest}"
    if provider_name in {"nowpayments", "plisio"}:
        from paurubot.payments.durable_checkout import start_hosted

        return await start_hosted(session, customer, provider_name, amount, storage_key)
    # Transaction-scoped PG lock: wait for the first request's commit, not a Redis TTL.
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": storage_key},
    )
    fingerprint: dict[str, str | int] = {"provider": provider_name, "amount": amount, "currency": "IRT"}
    payment = await session.scalar(select(Payment).where(Payment.idempotency_key == storage_key))
    if payment is not None:
        return replay(payment, customer, fingerprint)
    started = await engine._start(session, customer, provider_name, amount, storage_key=storage_key)
    started.payment.meta = {
        **(started.payment.meta or {}),
        META_KEY: {"request": fingerprint, "checkout": asdict(started.checkout)},
    }
    await session.flush()
    return started


def replay(payment: Payment, customer: Customer, fingerprint: dict[str, str | int]) -> engine.Started:
    """Replay only a complete, matching checkout; never recreate an uncertain attempt."""
    snapshot = (payment.meta or {}).get(META_KEY)
    if (
        payment.shop_id != customer.shop_id
        or payment.customer_id != customer.id
        or not isinstance(snapshot, dict)
        or snapshot.get("request") != fingerprint
    ):
        raise PaymentError("payment_intent_conflict")
    attempts = [(payment.meta or {}).get(key) for key in ("hosted_attempt_v1", "order_attempt_v1")]
    if any(isinstance(attempt, dict) and attempt.get("state") == "uncertain" for attempt in attempts):
        # Existing clients retain their key on gateway_error; closed/conflict clears it.
        # Never tell a client to replace the key for an ambiguous remote creation.
        raise PaymentError("gateway_error")
    if payment.status not in {
        PaymentStatus.PENDING,
        PaymentStatus.VERIFYING,
        PaymentStatus.PAID,
    }:
        raise PaymentError("payment_intent_closed")
    if payment.status != PaymentStatus.PAID and payment.expires_at and payment.expires_at <= utcnow():
        raise PaymentError("payment_intent_closed")
    checkout = snapshot.get("checkout")
    if not isinstance(checkout, dict):
        raise PaymentError("payment_intent_conflict")
    try:
        return engine.Started(payment, Checkout(**checkout))
    except TypeError as exc:
        raise PaymentError("payment_intent_conflict") from exc
