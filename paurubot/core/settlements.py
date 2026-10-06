"""Gateway settlements (PAY-03): what each payment method collected and what reached the bank.

Collected money sits on `gateway:<provider>` (negative balance = held by the gateway). Recording a
settlement posts bank -> gateway, so the gateway balance shows only what is still outstanding.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import LedgerAccount, LedgerAccountKind, Payment, PaymentStatus, Settlement
from paurubot.ledger.accounts import system_account
from paurubot.ledger.posting import transfer


@dataclass(slots=True)
class ProviderBalance:
    provider: str
    collected: int
    settled: int
    outstanding: int


async def balances(session: AsyncSession, shop_id: int) -> list[ProviderBalance]:
    collected = dict(
        (
            await session.execute(
                select(Payment.provider, func.coalesce(func.sum(Payment.amount), 0))
                .where(Payment.shop_id == shop_id, Payment.status == PaymentStatus.PAID)
                .group_by(Payment.provider)
            )
        ).all()
    )
    settled = dict(
        (
            await session.execute(
                select(Settlement.provider, func.coalesce(func.sum(Settlement.amount), 0))
                .where(Settlement.shop_id == shop_id)
                .group_by(Settlement.provider)
            )
        ).all()
    )
    gateway = {
        code.split(":", 1)[1]: int(balance)
        for code, balance in (
            await session.execute(
                select(LedgerAccount.code, LedgerAccount.balance).where(
                    LedgerAccount.shop_id == shop_id, LedgerAccount.kind == LedgerAccountKind.GATEWAY
                )
            )
        ).all()
    }
    names = sorted(set(collected) | set(settled) | {k for k, v in gateway.items() if v})
    return [
        ProviderBalance(
            provider=name,
            collected=int(collected.get(name, 0)),
            settled=int(settled.get(name, 0)),
            outstanding=-gateway.get(name, 0),
        )
        for name in names
    ]


async def record(
    session: AsyncSession,
    shop_id: int,
    *,
    provider: str,
    amount: int,
    reference: str | None,
    note: str | None,
    staff_id: int | None,
) -> Settlement:
    if amount <= 0:
        raise ValueError("settlement amount must be positive")
    bank = await system_account(session, shop_id, "bank")
    gateway = await system_account(session, shop_id, f"gateway:{provider}")
    posted = await transfer(
        session,
        shop_id=shop_id,
        source_id=bank.id,
        target_id=gateway.id,
        amount=amount,
        kind="settlement",
        idempotency_key=f"settlement:{shop_id}:{uuid.uuid4().hex}",
        memo=(note or reference or provider)[:255],
        actor=f"staff:{staff_id}" if staff_id else None,
    )
    row = Settlement(
        shop_id=shop_id,
        provider=provider,
        amount=amount,
        reference=reference,
        note=note,
        staff_id=staff_id,
        journal_id=posted.journal.id,
    )
    session.add(row)
    await session.flush()
    return row


async def history(session: AsyncSession, shop_id: int, limit: int = 50) -> list[Settlement]:
    rows = await session.scalars(
        select(Settlement).where(Settlement.shop_id == shop_id).order_by(Settlement.id.desc()).limit(limit)
    )
    return list(rows)
