"""Gift codes that charge the wallet (FIN-03)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, GiftCode, GiftRedemption, Order, OrderStatus
from paurubot.ledger import wallet

ATTEMPTS_PER_HOUR = 10


class GiftError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def redeem(session: AsyncSession, customer: Customer, text: str) -> int:
    """Credit a gift code once per customer and return the amount. Commits nothing."""
    # By Telegram account: a new user's failed try is rolled back with their customer row, so a
    # customer-id key would start over on every try.
    allowed, _ = await kv.hit(f"gift:{customer.shop_id}:{customer.telegram_id}", ATTEMPTS_PER_HOUR, 3600)
    if not allowed:
        raise GiftError("gift_too_many_attempts")
    code_text = text.strip()
    if not code_text or len(code_text) > 32:
        raise GiftError("gift_invalid")
    code = await session.scalar(
        select(GiftCode)
        .where(GiftCode.shop_id == customer.shop_id, func.lower(GiftCode.code) == code_text.lower())
        .with_for_update()
    )
    if code is None or not code.is_active:
        raise GiftError("gift_invalid")
    if code.expires_at and code.expires_at <= utcnow():
        raise GiftError("gift_expired")
    if code.max_uses is not None and code.used_count >= code.max_uses:
        raise GiftError("gift_used_up")
    if code.new_customers_only:
        bought = await session.scalar(
            select(func.count(Order.id)).where(
                Order.customer_id == customer.id, Order.status == OrderStatus.COMPLETED
            )
        )
        if bought:
            raise GiftError("gift_new_only")
    try:
        async with session.begin_nested():
            session.add(
                GiftRedemption(
                    shop_id=customer.shop_id, code_id=code.id, customer_id=customer.id, amount=code.amount
                )
            )
            await session.flush()
    except IntegrityError as exc:
        raise GiftError("gift_already_used") from exc
    code.used_count += 1
    await wallet.reward(
        session,
        customer.shop_id,
        customer.id,
        code.amount,
        key=f"gift:{code.id}:{customer.id}",
        memo=f"gift {code.code}",
    )
    return int(code.amount)
