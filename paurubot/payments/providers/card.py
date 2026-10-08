"""Card-to-card transfer with receipt upload and staff approval (PAY-04)."""

from __future__ import annotations

from typing import Any

from paurubot.db.models import Payment
from paurubot.payments.base import Checkout, PaymentProvider


class CardProvider(PaymentProvider):
    name = "card"
    manual_review = True

    async def enabled(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get("card_enabled") and active_accounts(cfg))

    async def create(self, payment: Payment, cfg: dict[str, Any]) -> Checkout:
        from paurubot.core.cards import pick_accounts

        accounts = pick_accounts(active_accounts(cfg), str(cfg.get("card_pick") or "all"), payment.id)
        if len(accounts) == 1:
            # Staff see which card this receipt should land on.
            payment.meta = {**(payment.meta or {}), "card": accounts[0]["number"][-4:]}
        return Checkout(instructions={"accounts": accounts, "amount": payment.amount})


def active_accounts(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = []
    for item in cfg.get("card_accounts") or []:
        if not isinstance(item, dict) or not item.get("number") or item.get("disabled"):
            continue
        accounts.append(
            {
                "number": str(item["number"]),
                "holder": str(item.get("holder", "")),
                "bank": str(item.get("bank", "")),
            }
        )
    return accounts
