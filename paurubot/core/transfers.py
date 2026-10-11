"""Moving wallet balance to another customer of the same shop (FIN-08)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import customers
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, LedgerAccount, LedgerEntry, LedgerJournal
from paurubot.ledger.accounts import wallet_account
from paurubot.ledger.posting import InsufficientFunds, transfer

MAX_AMOUNT = 100_000_000


class TransferError(Exception):
    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


async def find_recipient(session: AsyncSession, sender: Customer, text: str) -> Customer:
    """A Telegram ID or @username of someone who has started the bot."""
    query = text.strip()
    recipient: Customer | None = None
    if query.lstrip("-").isdigit():
        recipient = await customers.by_telegram(session, sender.shop_id, int(query))
    elif query.startswith("@") and len(query) > 1:
        recipient = await session.scalar(
            select(Customer).where(
                Customer.shop_id == sender.shop_id, func.lower(Customer.username) == query[1:].lower()
            )
        )
    if recipient is None or recipient.is_banned or recipient.telegram_id <= 0:
        raise TransferError("transfer_no_recipient")
    if recipient.id == sender.id:
        raise TransferError("transfer_self")
    return recipient


async def sent_today(session: AsyncSession, sender: Customer) -> int:
    wallet = await wallet_account(session, sender.shop_id, sender.id)
    total = await session.scalar(
        select(func.coalesce(func.sum(-LedgerEntry.amount), 0))
        .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
        .where(
            LedgerEntry.account_id == wallet.id,
            LedgerEntry.amount < 0,
            LedgerJournal.kind == "transfer",
            LedgerEntry.created_at >= utcnow() - timedelta(days=1),
        )
    )
    return int(total or 0)


async def check(session: AsyncSession, sender: Customer, amount: int) -> None:
    cfg = await shop_settings.load(session, sender.shop_id)
    if not cfg["wallet_transfer_enabled"]:
        raise TransferError("transfer_off")
    if sender.is_banned:
        raise TransferError("transfer_off")
    minimum = int(cfg["wallet_transfer_min"] or 0)
    if amount < max(1, minimum):
        raise TransferError("transfer_min", low=minimum)
    if amount > MAX_AMOUNT:
        raise TransferError("transfer_max", high=MAX_AMOUNT)
    daily = int(cfg["wallet_transfer_daily_max"] or 0)
    if daily and await sent_today(session, sender) + amount > daily:
        raise TransferError("transfer_daily_max", high=daily)


async def send(
    session: AsyncSession, sender: Customer, recipient: Customer, amount: int, *, key: str
) -> None:
    """Move `amount` from the sender's wallet to the recipient's, once per `key`."""
    if recipient.shop_id != sender.shop_id or recipient.id == sender.id:
        raise TransferError("transfer_no_recipient")
    source = await wallet_account(session, sender.shop_id, sender.id)
    target = await wallet_account(session, sender.shop_id, recipient.id)
    # Lock both wallets (in id order, like the ledger does) before reading today's total, so two
    # transfers sent at the same moment are checked one after the other against the daily cap.
    await session.execute(
        select(LedgerAccount.id)
        .where(LedgerAccount.id.in_(sorted({source.id, target.id})))
        .order_by(LedgerAccount.id)
        .with_for_update()
    )
    done = await session.scalar(
        select(LedgerJournal.id).where(LedgerJournal.idempotency_key == f"transfer:{key}")
    )
    if done is not None:
        return  # the same confirmation pressed again
    await check(session, sender, amount)
    try:
        await transfer(
            session,
            shop_id=sender.shop_id,
            source_id=source.id,
            target_id=target.id,
            amount=amount,
            kind="transfer",
            idempotency_key=f"transfer:{key}",
            reference_type="customer",
            reference_id=recipient.id,
            memo=f"to customer {recipient.id}",
            actor=f"customer:{sender.id}",
        )
    except InsufficientFunds as exc:
        raise TransferError("insufficient_balance", balance=exc.balance, required=amount) from exc
