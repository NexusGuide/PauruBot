"""Stars charges Telegram made but the bot never heard of (SEC-F05).

Telegram keeps an undelivered update for 24 hours. A bot that was down longer, or a
successful_payment lost on the way, left the customer charged and the wallet uncredited. The
bot's own transaction list (getStarTransactions) is the record of every charge, so a periodic
pass reads it and credits each invoice payment of ours that is not credited yet, exactly as the
message would have: same payment, same customer, same amount, the charge id as the transaction
id (so nothing is credited twice). Anything that does not match is reported, never guessed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.models import Customer, Payment, PaymentStatus

log = logging.getLogger(__name__)

PAGE = 100
MAX_PAGES = 5
OVERLAP = 50  # read a little behind the cursor: cheap, and safe if a page was half handled
CURSOR_TTL = 90 * 86400


async def _unmatched(result: Pass, shop_id: int, tx_id: str) -> None:
    """Report a charge once, however many passes read it again."""
    if await kv.set_once(f"stars:reported:{shop_id}:{tx_id}", CURSOR_TTL):
        result.unmatched.append(tx_id)


@dataclass(slots=True)
class Pass:
    credited: list[int] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    read: int = 0


def _cursor_key(shop_id: int) -> str:
    return f"stars:offset:{shop_id}"


def _payment_id(payload: str | None) -> int | None:
    text = payload or ""
    return int(text[4:]) if text.startswith("pay:") and text[4:].isdigit() else None


async def _settle(session: AsyncSession, shop_id: int, tx: Any, result: Pass) -> None:
    from paurubot.payments import engine

    source = getattr(tx, "source", None)
    if (
        getattr(source, "type", None) != "user"
        or getattr(source, "transaction_type", None) != "invoice_payment"
    ):
        return  # gifts, refunds, ads and the like are not ours to credit
    payment_id = _payment_id(getattr(source, "invoice_payload", None))
    user = getattr(source, "user", None)
    payment = await session.get(Payment, payment_id) if payment_id else None
    if payment is None or payment.shop_id != shop_id or payment.provider != "stars":
        await _unmatched(result, shop_id, str(tx.id))
        return
    if payment.provider_tx_id == str(tx.id):
        return  # this very charge is credited, by the message or an earlier pass
    customer = await session.get(Customer, payment.customer_id)
    if (
        customer is None
        or user is None
        or customer.telegram_id != user.id
        or payment.status == PaymentStatus.PAID
    ):
        # Someone else paid this invoice, or it was paid twice: a real charge with no payment
        # left to credit. A person refunds or credits it.
        await _unmatched(result, shop_id, str(tx.id))
        return
    ok = await engine.verify(
        session, payment.id, {"currency": "XTR", "total_amount": int(tx.amount), "charge_id": str(tx.id)}
    )
    await session.commit()
    if ok:
        await engine.fulfill_one(session, payment.id)
        result.credited.append(payment.id)
    elif payment.status != PaymentStatus.PAID:
        await _unmatched(result, shop_id, str(tx.id))  # wrong amount, a reused charge: a person decides


async def reconcile_shop(session: AsyncSession, shop_id: int, bot: Any) -> Pass:
    """Read the shop bot's Star transactions from where the last pass stopped. Commits."""
    redis = kv.get_redis()
    stored = await redis.get(_cursor_key(shop_id))
    offset = max(0, int(stored) - OVERLAP) if stored and str(stored).isdigit() else 0
    result = Pass()
    for _ in range(MAX_PAGES):
        page = await bot.get_star_transactions(offset=offset, limit=PAGE)
        transactions = list(getattr(page, "transactions", None) or [])
        for tx in transactions:
            await _settle(session, shop_id, tx, result)
        result.read += len(transactions)
        offset += len(transactions)
        await redis.set(_cursor_key(shop_id), str(offset), ex=CURSOR_TTL)
        if len(transactions) < PAGE:
            break
    return result
