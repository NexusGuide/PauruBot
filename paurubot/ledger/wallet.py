"""Wallet operations expressed as ledger journals (FIN-01)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import LedgerAccount, LedgerEntry, LedgerJournal
from paurubot.ledger.accounts import system_account, wallet_account
from paurubot.ledger.posting import InsufficientFunds, Posted, transfer

__all__ = ["InsufficientFunds", "adjust", "charge", "history", "refund", "reward", "topup"]


async def topup(
    session: AsyncSession, shop_id: int, customer_id: int, amount: int, *, provider: str, payment_id: int
) -> Posted:
    wallet = await wallet_account(session, shop_id, customer_id)
    gateway = await system_account(session, shop_id, f"gateway:{provider}")
    return await transfer(
        session,
        shop_id=shop_id,
        source_id=gateway.id,
        target_id=wallet.id,
        amount=amount,
        kind="topup",
        idempotency_key=f"payment:{payment_id}:credit",
        reference_type="payment",
        reference_id=payment_id,
    )


async def charge(
    session: AsyncSession, shop_id: int, customer_id: int, amount: int, *, order_id: int
) -> Posted | None:
    if amount <= 0:
        return None
    wallet = await wallet_account(session, shop_id, customer_id)
    revenue = await system_account(session, shop_id, "revenue")
    return await transfer(
        session,
        shop_id=shop_id,
        source_id=wallet.id,
        target_id=revenue.id,
        amount=amount,
        kind="charge",
        idempotency_key=f"order:{order_id}:charge",
        reference_type="order",
        reference_id=order_id,
    )


async def refund(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    amount: int,
    *,
    order_id: int,
    actor: str | None = None,
) -> Posted | None:
    if amount <= 0:
        return None
    wallet = await wallet_account(session, shop_id, customer_id)
    revenue = await system_account(session, shop_id, "revenue")
    return await transfer(
        session,
        shop_id=shop_id,
        source_id=revenue.id,
        target_id=wallet.id,
        amount=amount,
        kind="refund",
        idempotency_key=f"order:{order_id}:refund",
        reference_type="order",
        reference_id=order_id,
        actor=actor,
    )


async def reward(
    session: AsyncSession, shop_id: int, customer_id: int, amount: int, *, key: str, memo: str | None = None
) -> Posted:
    wallet = await wallet_account(session, shop_id, customer_id)
    promotions = await system_account(session, shop_id, "promotions")
    return await transfer(
        session,
        shop_id=shop_id,
        source_id=promotions.id,
        target_id=wallet.id,
        amount=amount,
        kind="reward",
        idempotency_key=key,
        memo=memo,
    )


async def adjust(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    delta: int,
    *,
    key: str,
    actor: str,
    memo: str | None = None,
) -> Posted:
    wallet = await wallet_account(session, shop_id, customer_id)
    adjustments = await system_account(session, shop_id, "adjustments")
    if delta > 0:
        source, target, amount = adjustments.id, wallet.id, delta
    else:
        source, target, amount = wallet.id, adjustments.id, -delta
    return await transfer(
        session,
        shop_id=shop_id,
        source_id=source,
        target_id=target,
        amount=amount,
        kind="adjustment",
        idempotency_key=key,
        memo=memo,
        actor=actor,
    )


async def history(
    session: AsyncSession, shop_id: int, customer_id: int, *, limit: int = 20, before_id: int | None = None
) -> list[tuple[LedgerEntry, LedgerJournal]]:
    account = await session.scalar(
        select(LedgerAccount).where(
            LedgerAccount.shop_id == shop_id, LedgerAccount.customer_id == customer_id
        )
    )
    if account is None:
        return []
    stmt = (
        select(LedgerEntry, LedgerJournal)
        .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
        .where(LedgerEntry.account_id == account.id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
    )
    if before_id:
        stmt = stmt.where(LedgerEntry.id < before_id)
    return [(entry, journal) for entry, journal in (await session.execute(stmt)).all()]
