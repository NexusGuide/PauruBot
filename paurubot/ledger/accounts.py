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
        .on_conflict_do_nothing(index_elements=["shop_id", "code"])
    )
    await session.execute(stmt)
    account = await session.scalar(
        select(LedgerAccount).where(LedgerAccount.shop_id == shop_id, LedgerAccount.code == code)
    )
    assert account is not None
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
