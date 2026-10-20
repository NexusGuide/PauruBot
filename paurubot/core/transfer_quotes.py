"""Short-lived, principal-bound transfer consent; PostgreSQL journals remain the truth."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.core import transfers
from paurubot.db.models import Customer, LedgerAccount, LedgerJournal
from paurubot.ledger.accounts import wallet_account, wallet_balance

TTL = 300


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(1, ge=1, le=1)
    shop: int = Field(gt=0)
    sender: int = Field(gt=0)
    recipient: int = Field(gt=0)
    amount: int = Field(gt=0, le=transfers.MAX_AMOUNT)
    currency: str = Field(min_length=1, max_length=16)
    expires: int = Field(gt=0)
    nonce: str = Field(pattern=r"^[a-f0-9]{32}$")


def _key() -> bytes:
    master = get_settings().master_key.get_secret_value()
    if len(master) < 32:
        raise transfers.TransferError("transfer_unavailable")
    return hmac.digest(master.encode(), b"paurubot:wallet-transfer:v1", "sha256")


def encode(consent: Consent) -> str:
    raw = base64.urlsafe_b64encode(consent.model_dump_json().encode()).decode().rstrip("=")
    return raw + "." + hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()


def decode(token: str, shop_id: int, sender_id: int, currency: str) -> Consent:
    try:
        if len(token) > 2048:
            raise ValueError("size")
        raw, signature = token.split(".")
        expected = hmac.new(_key(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        consent = Consent.model_validate_json(
            base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        )
        if encode(consent) != token:
            raise ValueError("noncanonical")
        if (consent.shop, consent.sender, consent.currency) != (
            shop_id,
            sender_id,
            currency,
        ):
            raise ValueError("principal")
        return consent
    except (ValueError, ValidationError, UnicodeError, TypeError) as exc:
        raise transfers.TransferError("transfer_quote_invalid") from exc


def journal_key(consent: Consent) -> str:
    # Hash the entire validated payload, not client-controlled raw token spelling.
    digest = hashlib.sha256(consent.model_dump_json().encode()).hexdigest()
    return f"webapp:{consent.shop}:{consent.sender}:{digest}"


async def completed(session: AsyncSession, consent: Consent) -> int | None:
    return await session.scalar(
        select(LedgerJournal.id).where(
            LedgerJournal.idempotency_key == f"transfer:{journal_key(consent)}",
            LedgerJournal.shop_id == consent.shop,
            LedgerJournal.kind == "transfer",
            LedgerJournal.reference_type == "customer",
            LedgerJournal.reference_id == consent.recipient,
        )
    )


async def quote(
    session: AsyncSession,
    sender: Customer,
    recipient_text: str,
    amount: int,
    currency: str,
) -> dict[str, Any]:
    await shop_settings.load(session, sender.shop_id, fresh=True)
    await transfers.check(session, sender, amount)
    recipient = await transfers.find_recipient(session, sender, recipient_text)
    balance = await wallet_balance(session, sender.shop_id, sender.id)
    if amount > balance:
        raise transfers.TransferError("insufficient_balance", balance=balance, required=amount)
    consent = Consent(
        shop=sender.shop_id,
        sender=sender.id,
        recipient=recipient.id,
        amount=amount,
        currency=currency,
        expires=int(time.time()) + TTL,
        nonce=secrets.token_hex(16),
    )
    return {
        "token": encode(consent),
        "amount": amount,
        "currency": currency,
        "expires_at": consent.expires,
        "recipient": {
            "telegram_id": recipient.telegram_id,
            "name": recipient.first_name,
            "username": recipient.username,
        },
    }


async def confirm(session: AsyncSession, sender: Customer, token: str, currency: str) -> dict[str, Any]:
    consent = decode(token, sender.shop_id, sender.id, currency)
    # A successful result remains readable after consent expires. Never redo the money movement.
    prior = await completed(session, consent)
    if prior is not None:
        return {"journal_id": prior, "amount": consent.amount, "replayed": True}
    recipient = await session.get(Customer, consent.recipient)
    if recipient is None or recipient.shop_id != sender.shop_id or recipient.id == sender.id:
        raise transfers.TransferError("transfer_no_recipient")
    accounts = [
        await wallet_account(session, sender.shop_id, c.id)
        for c in sorted((sender, recipient), key=lambda c: c.id)
    ]
    await session.execute(
        select(LedgerAccount.id)
        .where(LedgerAccount.id.in_([a.id for a in accounts]))
        .order_by(LedgerAccount.id)
        .with_for_update()
    )
    # Waiters must see a committed winner before checking the now-stale quota/expiry.
    prior = await completed(session, consent)
    if prior is not None:
        return {"journal_id": prior, "amount": consent.amount, "replayed": True}
    if consent.expires <= int(time.time()):
        raise transfers.TransferError("transfer_quote_expired")
    await session.refresh(sender, ["is_banned"])
    await session.refresh(recipient, ["is_banned", "telegram_id", "shop_id"])
    if (
        sender.is_banned
        or recipient.is_banned
        or recipient.telegram_id <= 0
        or recipient.shop_id != sender.shop_id
    ):
        raise transfers.TransferError("transfer_no_recipient")
    await shop_settings.load(session, sender.shop_id, fresh=True)
    # The shared core re-reads shop policy and enforces rolling daily cap under these same locks.
    await transfers.send(session, sender, recipient, consent.amount, key=journal_key(consent))
    journal_id = await completed(session, consent)
    assert journal_id is not None
    return {"journal_id": journal_id, "amount": consent.amount, "replayed": False}
