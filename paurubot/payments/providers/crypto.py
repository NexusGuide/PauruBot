"""Direct on-chain payments in USDT-TRC20, TRX and TON (PAY-11).

Each payment asks for a unique amount (the price plus a few base units), so an incoming transfer to
the shop's address identifies its payment without a memo. A transfer counts only when it is
confirmed, sent to the configured address on the right network and token, for exactly that amount,
and its transaction hash has never been used before.

The few base units are *reserved* (SEC-F18): under a lock on the network and address, a payment
takes the first slot no other still-matchable payment to that address holds, starting from the one
its ID suggests. Two open payments never ask for the same amount, so one transfer can never match
both and leave the customer's money waiting for review. When every slot is taken, the method
answers `method_busy` instead of handing out a duplicate.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_object_session

from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.payments import fx
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider
from paurubot.payments.chains import MATCH_WINDOW, NETWORKS, valid_address

log = logging.getLogger(__name__)
# Statuses whose amount an incoming transfer may still be matched against (see payments.watch).
MATCHABLE = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.EXPIRED)

DUST_SLOTS = 9973


def address_for(network: str, cfg: dict[str, Any]) -> str:
    key = "ton_address" if NETWORKS[network]["chain"] == "ton" else "tron_address"
    return str(cfg.get(key) or "").strip()


def base_amount(toman: int, rate_toman: int, decimals: int) -> int:
    """Smallest whole number of base units worth at least `toman`."""
    scale = 10**decimals
    return -(-toman * scale // rate_toman)


def unique_amount(base: int, payment_id: int, decimals: int) -> int:
    unit = 10 ** max(0, decimals - 5)
    return base + (payment_id % DUST_SLOTS + 1) * unit


async def reserve_amount(
    session: AsyncSession, network: str, address: str, base: int, payment: Payment, decimals: int
) -> int:
    """The first amount in `base`'s slots that no still-matchable payment to `address` asks for.

    Holds a transaction lock on (network, address) until the caller commits, so two payments
    created at once cannot both take the same slot. The address, not the shop, is the scope:
    shops sharing an address share its transfers.
    """
    unit = 10 ** max(0, decimals - 5)
    key = func.hashtextextended(f"paurubot:crypto-amount:{network}:{address}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))
    taken = set(
        await session.scalars(
            select(Payment.provider_amount).where(
                Payment.provider == network,
                Payment.provider_ref == address,
                Payment.status.in_(MATCHABLE),
                Payment.created_at >= utcnow() - MATCH_WINDOW,
                Payment.id != payment.id,
                Payment.provider_amount.between(base + unit, base + DUST_SLOTS * unit),
            )
        )
    )
    first = payment.id % DUST_SLOTS
    for step in range(DUST_SLOTS):
        amount = base + ((first + step) % DUST_SLOTS + 1) * unit
        if amount not in taken:
            return amount
    log.warning("every amount slot for %s on %s is taken; refusing a duplicate", network, address[:8])
    raise PaymentError("method_busy")


def display(amount: int, decimals: int) -> str:
    text = format(Decimal(amount) / (Decimal(10) ** decimals), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class CryptoProvider(PaymentProvider):
    def __init__(self, network: str) -> None:
        self.network = network
        self.name = network
        self.meta = NETWORKS[network]

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        if not cfg.get("crypto_enabled") or self.network not in (cfg.get("crypto_networks") or []):
            return False
        if not valid_address(self.network, address_for(self.network, cfg)):
            return False
        try:
            await fx.rate(cfg, self.meta["symbol"])
        except fx.RateUnavailable:
            return False
        return True

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        address = address_for(self.network, cfg)
        if not valid_address(self.network, address):
            raise PaymentError("method_disabled")
        try:
            rate = await fx.rate(cfg, self.meta["symbol"])
        except fx.RateUnavailable as exc:
            raise PaymentError("rate_unavailable") from exc
        decimals = int(self.meta["decimals"])
        base = base_amount(payment.amount, rate, decimals)
        session = async_object_session(payment)
        if session is None:
            # Every caller creates payments inside a transaction; never guess outside one.
            raise PaymentError("method_disabled")
        amount = await reserve_amount(session, self.network, address, base, payment, decimals)
        expires: datetime | None = payment.expires_at
        return Checkout(
            instructions={
                "network": self.network,
                "label": self.meta["label"],
                "address": address,
                "amount": display(amount, decimals),
                "memo": f"P{payment.id}" if self.meta["chain"] == "ton" else None,
                "rate": rate,
                "expires_at": expires.isoformat() if expires else None,
            },
            provider_ref=address,
            provider_amount=amount,
            provider_currency=self.network,
        )


PROVIDERS = {name: CryptoProvider(name) for name in NETWORKS}
