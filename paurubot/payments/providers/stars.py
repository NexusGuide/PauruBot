"""Telegram Stars invoices (PAY-12). Telegram reports the charge; we check amount and currency."""

from __future__ import annotations

import math
from typing import Any

from paurubot.db.models import Payment
from paurubot.payments.base import Checkout, PaymentProvider, Verification


def stars_for(amount: int, rate: int) -> int:
    return max(1, math.ceil(amount / max(1, rate)))


class StarsProvider(PaymentProvider):
    name = "stars"
    # A valid Telegram successful_payment can arrive after the local expiry sweep.
    # engine.verify still enforces its late window and transaction uniqueness.
    late_ok = True
    # Telegram's successful_payment, or the bot's own transaction list, is the charge itself: a
    # Stars payment is credited however late it is seen (SEC-F05), never only within two days.
    late_unbounded = True

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get("stars_enabled") and int(cfg.get("stars_rate") or 0) > 0)

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        stars = stars_for(payment.amount, int(cfg["stars_rate"]))
        return Checkout(
            instructions={"payload": f"pay:{payment.id}", "stars": stars},
            provider_amount=stars,
            provider_currency="XTR",
        )

    async def verify(self, payment: Payment, cfg: dict[str, Any], params: dict[str, Any]) -> Verification:
        if params.get("currency") != "XTR" or int(params.get("total_amount") or 0) != payment.provider_amount:
            return Verification(False, detail="amount or currency mismatch")
        charge_id = str(params.get("charge_id") or "")
        if not charge_id:
            return Verification(False, detail="missing charge id")
        return Verification(True, provider_tx_id=charge_id, amount=payment.amount, currency=payment.currency)
