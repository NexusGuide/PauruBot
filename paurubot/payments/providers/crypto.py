"""Direct on-chain payments in USDT-TRC20, TRX and TON (PAY-11).

Each payment asks for a unique amount (the price plus a few base units derived from the payment ID),
so an incoming transfer to the shop's address identifies its payment without a memo. A transfer
counts only when it is confirmed, sent to the configured address on the right network and token,
for exactly that amount, and its transaction hash has never been used before.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from paurubot.db.models import Payment
from paurubot.payments import fx
from paurubot.payments.base import Checkout, PaymentError, PaymentProvider
from paurubot.payments.chains import NETWORKS, valid_address

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
        amount = unique_amount(base_amount(payment.amount, rate, decimals), payment.id, decimals)
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
