"""Referral rewards on a referred customer's first paid order (GRW-01), with limits against
farming fake referrals (GRW-09): a minimum order and a daily cap per referrer."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, LedgerEntry, LedgerJournal, Order, OrderKind
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_account


async def rewards_today(session: AsyncSession, shop_id: int, referrer_id: int) -> int:
    account = await wallet_account(session, shop_id, referrer_id)
    count = await session.scalar(
        select(func.count(LedgerJournal.id))
        .join(LedgerEntry, LedgerEntry.journal_id == LedgerJournal.id)
        .where(
            LedgerEntry.account_id == account.id,
            LedgerJournal.idempotency_key.like("referral:%"),
            LedgerJournal.created_at >= utcnow() - timedelta(days=1),
        )
    )
    return int(count or 0)


async def reward_referrer(session: AsyncSession, order: Order) -> int:
    if order.kind == OrderKind.TRIAL or order.amount <= 0:
        return 0
    customer = await session.get(Customer, order.customer_id)
    if customer is None or customer.referrer_id is None or customer.referral_rewarded:
        return 0
    cfg = await shop_settings.load(session, order.shop_id)
    if not cfg["referral_enabled"]:
        return 0
    if order.amount < int(cfg["referral_min_order"] or 0):
        return 0  # a later, larger first purchase still counts
    referrer = await session.get(Customer, customer.referrer_id)
    if referrer is None or referrer.is_banned:
        return 0
    daily = int(cfg["referral_daily_max"] or 0)
    if daily and await rewards_today(session, order.shop_id, referrer.id) >= daily:
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
