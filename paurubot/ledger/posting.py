"""Double-entry postings on the append-only ledger (FIN-02).

Every journal sums to zero. Wallet accounts may never go negative. Balances on
`ledger_accounts` are a projection maintained in the same transaction under row locks.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import LedgerAccount, LedgerEntry, LedgerJournal


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    def __init__(self, account_id: int, balance: int, required: int) -> None:
        super().__init__(f"account {account_id}: balance {balance} < {required}")
        self.account_id = account_id
        self.balance = balance
        self.required = required


@dataclass(slots=True, frozen=True)
class Line:
    account_id: int
    amount: int


@dataclass(slots=True)
class Posted:
    journal: LedgerJournal
    created: bool


async def post(
    session: AsyncSession,
    *,
    shop_id: int,
    kind: str,
    idempotency_key: str,
    lines: list[Line],
    reference_type: str | None = None,
    reference_id: int | None = None,
    memo: str | None = None,
    actor: str | None = None,
    reverses_id: int | None = None,
) -> Posted:
    """Post once; replay a key only for the same financial effect and business reference."""
    merged: dict[int, int] = defaultdict(int)
    for line in lines:
        if not isinstance(line.amount, int) or isinstance(line.amount, bool):
            raise LedgerError("amounts must be integers")
        merged[line.account_id] += line.amount
    merged = {acc: amt for acc, amt in merged.items() if amt != 0}
    if not merged:
        raise LedgerError("journal has no effect")
    if sum(merged.values()) != 0:
        raise LedgerError("journal does not balance")

    # A savepoint keeps a rejected journal (e.g. insufficient funds) from leaking into the caller.
    async with session.begin_nested():
        journal_id = await session.scalar(
            insert(LedgerJournal)
            .values(
                shop_id=shop_id,
                kind=kind,
                idempotency_key=idempotency_key,
                reference_type=reference_type,
                reference_id=reference_id,
                memo=memo,
                actor=actor,
                reverses_id=reverses_id,
                created_at=utcnow(),
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(LedgerJournal.id)
        )
        if journal_id is None:
            existing = await session.scalar(
                select(LedgerJournal).where(LedgerJournal.idempotency_key == idempotency_key)
            )
            assert existing is not None
            if (
                existing.shop_id != shop_id
                or existing.kind != kind
                or existing.reference_type != reference_type
                or existing.reference_id != reference_id
                or existing.reverses_id != reverses_id
            ):
                raise LedgerError("idempotency key reused for a different journal")
            # Compare canonical effects against the immutable entries, including old
            # journals: no fingerprint migration and no trust in retry-supplied amounts.
            previous: dict[int, int] = defaultdict(int)
            for account_id, amount in await session.execute(
                select(LedgerEntry.account_id, LedgerEntry.amount).where(
                    LedgerEntry.journal_id == existing.id
                )
            ):
                previous[account_id] += amount
            previous = {acc: amt for acc, amt in previous.items() if amt != 0}
            if previous != merged:
                raise LedgerError("idempotency key reused for a different journal")
            return Posted(existing, False)

        # Lock in a stable order so concurrent journals touching the same accounts cannot deadlock.
        accounts = {
            acc.id: acc
            for acc in await session.scalars(
                select(LedgerAccount)
                .where(LedgerAccount.id.in_(sorted(merged)))
                .order_by(LedgerAccount.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        }
        if len(accounts) != len(merged):
            raise LedgerError("unknown ledger account")
        for account_id, amount in merged.items():
            account = accounts[account_id]
            if account.shop_id != shop_id:
                raise LedgerError("cross-shop posting is not allowed")
            new_balance = account.balance + amount
            if new_balance < 0 and not account.allow_negative:
                raise InsufficientFunds(account.id, account.balance, -amount)
        for account_id in sorted(merged):
            account = accounts[account_id]
            account.balance += merged[account_id]
            session.add(
                LedgerEntry(
                    journal_id=journal_id,
                    account_id=account_id,
                    amount=merged[account_id],
                    balance_after=account.balance,
                    created_at=utcnow(),
                )
            )
        await session.flush()
    journal = await session.get(LedgerJournal, journal_id)
    assert journal is not None
    return Posted(journal, True)


async def transfer(
    session: AsyncSession,
    *,
    shop_id: int,
    source_id: int,
    target_id: int,
    amount: int,
    kind: str,
    idempotency_key: str,
    reference_type: str | None = None,
    reference_id: int | None = None,
    memo: str | None = None,
    actor: str | None = None,
) -> Posted:
    if amount <= 0:
        raise LedgerError("transfer amount must be positive")
    return await post(
        session,
        shop_id=shop_id,
        kind=kind,
        idempotency_key=idempotency_key,
        lines=[Line(source_id, -amount), Line(target_id, amount)],
        reference_type=reference_type,
        reference_id=reference_id,
        memo=memo,
        actor=actor,
    )


async def reverse(
    session: AsyncSession,
    journal_id: int,
    *,
    idempotency_key: str,
    memo: str | None = None,
    actor: str | None = None,
) -> Posted:
    journal = await session.get(LedgerJournal, journal_id)
    if journal is None:
        raise LedgerError("journal not found")
    entries = list(await session.scalars(select(LedgerEntry).where(LedgerEntry.journal_id == journal_id)))
    return await post(
        session,
        shop_id=journal.shop_id,
        kind=f"reversal:{journal.kind}"[:32],
        idempotency_key=idempotency_key,
        lines=[Line(entry.account_id, -entry.amount) for entry in entries],
        reference_type=journal.reference_type,
        reference_id=journal.reference_id,
        memo=memo or f"reverses journal {journal.id}",
        actor=actor,
        reverses_id=journal.id,
    )


@dataclass(slots=True)
class Drift:
    account_id: int
    code: str
    cached: int
    actual: int


async def reconcile(session: AsyncSession, shop_id: int | None = None) -> list[Drift]:
    """Compare cached balances with the sum of entries; any result is an incident."""
    sums = (
        select(LedgerEntry.account_id, func.coalesce(func.sum(LedgerEntry.amount), 0).label("total"))
        .group_by(LedgerEntry.account_id)
        .subquery()
    )
    stmt = select(LedgerAccount, func.coalesce(sums.c.total, 0)).outerjoin(
        sums, sums.c.account_id == LedgerAccount.id
    )
    if shop_id is not None:
        stmt = stmt.where(LedgerAccount.shop_id == shop_id)
    drift: list[Drift] = []
    for account, total in (await session.execute(stmt)).all():
        if int(total) != int(account.balance):
            drift.append(Drift(account.id, account.code, int(account.balance), int(total)))
    unbalanced = await session.execute(
        select(LedgerEntry.journal_id)
        .group_by(LedgerEntry.journal_id)
        .having(func.sum(LedgerEntry.amount) != 0)
    )
    for (journal_id,) in unbalanced.all():
        drift.append(Drift(-1, f"journal:{journal_id}", 0, 0))
    return drift
