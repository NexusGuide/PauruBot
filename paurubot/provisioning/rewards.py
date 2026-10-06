"""Referral rewards on a referred customer's first paid order (GRW-01)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Order, OrderKind
from paurubot.ledger import wallet


async def reward_referrer(session: AsyncSession, order: Order) -> int:
    if order.kind == OrderKind.TRIAL or order.amount <= 0:
        return 0
    customer = await session.get(Customer, order.customer_id)
    if customer is None or customer.referrer_id is None or customer.referral_rewarded:
        return 0
    cfg = await shop_settings.load(session, order.shop_id)
    if not cfg["referral_enabled"]:
        return 0
    reward = order.amount * int(cfg["referral_percent"] or 0) // 100
    customer.referral_rewarded = True
    if reward <= 0:
        return 0
    await wallet.reward(
        session,
        order.shop_id,
        customer.referrer_id,
        reward,
        key=f"referral:{customer.id}",
        memo=f"referral reward for customer {customer.id}",
    )
    from paurubot.worker import queue

    await queue.enqueue(
        session,
        "notify",
        "referral_reward",
        {"customer_id": customer.referrer_id, "amount": reward},
        shop_id=order.shop_id,
        dedupe_key=f"referral:{customer.id}:notify",
    )
    return reward
