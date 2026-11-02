from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import LedgerAccount, LedgerAccountKind

SYSTEM_ACCOUNTS: dict[str, LedgerAccountKind] = {
    "revenue": LedgerAccountKind.REVENUE,
    "promotions": LedgerAccountKind.PROMOTIONS,
    "adjustments": LedgerAccountKind.ADJUSTMENTS,
    "refunds": LedgerAccountKind.REFUNDS,
    "bank": LedgerAccountKind.BANK,
    "commissions": LedgerAccountKind.COMMISSIONS,
    "gateway:card": LedgerAccountKind.GATEWAY,
    "gateway:zarinpal": LedgerAccountKind.GATEWAY,
    "gateway:stars": LedgerAccountKind.GATEWAY,
    "gateway:admin": LedgerAccountKind.GATEWAY,
}


async def _ensure(
    session: AsyncSession, shop_id: int, code: str, kind: LedgerAccountKind, customer_id: int | None = None
) -> LedgerAccount:
    # Existing accounts need only an MVCC read. Re-inserting an existing unique key
    # can wait for another transaction's balance update, even with DO NOTHING.
    # Combined with creation of a missing counterparty wallet, that inverts the
    # transfer's row-lock order and can form a speculative-insert deadlock.
    existing = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.shop_id == shop_id, LedgerAccount.code == code)
    )
    if existing is not None:
        return existing
    # Missing-account races still resolve through the unique indexes, followed by
    # a fresh read of the winner. Posting remains responsible for balance locks.
    # No conflict target: a wallet row is unique on (shop_id, code) AND on customer_id.
    # A concurrent creator can show up first on either index (index entries are
    # inserted one at a time); an arbiter on one index alone turns the other into a
    # spurious unique violation instead of "someone else created it".
    stmt = (
        insert(LedgerAccount)
        .values(
            shop_id=shop_id,
            code=code,
            kind=kind,
            customer_id=customer_id,
            balance=0,
            allow_negative=kind != LedgerAccountKind.WALLET,
        )
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)
    account = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.shop_id == shop_id, LedgerAccount.code == code)
    )
    if account is None:
        # The conflict was on customer_id under another (shop, code): never guess which wallet.
        raise RuntimeError(f"ledger account {code} conflicts with another account of the same customer")
    return account


async def ensure_system_accounts(session: AsyncSession, shop_id: int) -> None:
    for code, kind in SYSTEM_ACCOUNTS.items():
        await _ensure(session, shop_id, code, kind)


async def system_account(session: AsyncSession, shop_id: int, code: str) -> LedgerAccount:
    kind = SYSTEM_ACCOUNTS.get(code)
    if kind is None:
        if code.startswith("gateway:"):
            kind = LedgerAccountKind.GATEWAY
        else:
            raise KeyError(code)
    return await _ensure(session, shop_id, code, kind)


async def wallet_account(session: AsyncSession, shop_id: int, customer_id: int) -> LedgerAccount:
    return await _ensure(session, shop_id, f"wallet:{customer_id}", LedgerAccountKind.WALLET, customer_id)


async def wallet_balance(session: AsyncSession, shop_id: int, customer_id: int) -> int:
    value = await session.scalar(
        select(LedgerAccount.balance).where(
            LedgerAccount.shop_id == shop_id, LedgerAccount.customer_id == customer_id
        )
    )
    return int(value or 0)
