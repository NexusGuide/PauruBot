"""Card-to-card payments confirmed by the bank's own deposit SMS (REF-02).

A phone that receives the shop's bank SMS runs an SMS forwarder app, which posts each message to
this shop's webhook. A deposit SMS whose amount equals exactly one open card payment approves it,
receipt or not; the money has reached the account, and the bank says so. Anything less certain
(no amount found, no payment or several payments with that amount, a message seen before) changes
nothing and is reported to staff, who review the receipt as before.

Exact amounts are what make this safe, so while SMS confirmation is on every new card payment of
the shop asks for an amount no other open card payment uses (`reserve_amount`), the same way
on-chain amounts are reserved (SEC-F18).

The parser is deliberately strict and independent of any one bank's layout: it reads the amount
marked as a deposit (a ``+`` sign, or a deposit word on the amount's line), skips balance, date,
time and account lines, and gives up when more than one amount qualifies.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.formatting import to_latin_digits
from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus

WORDS_FILE = Path(__file__).with_name("bank_sms.json")
OPEN = (PaymentStatus.PENDING, PaymentStatus.VERIFYING)
TAIL_MAX = 999
TEXT_MAX = 2000
SECRET_MIN = 24
# Arabic letters some phones and banks use, mapped to the Persian ones the word list uses.
_LETTERS = str.maketrans({"\u064a": "\u06cc", "\u0643": "\u06a9", "\u0629": "\u0647", "\u200c": " "})
# Lines forwarder apps append (SIM slot, device), never part of the bank's message.
_FOOTER = re.compile(r"\n\s*SIM\d*[_ :].*$", re.IGNORECASE | re.DOTALL)
_DATE = re.compile(r"\d{2,4}\s*[/\-.]\s*\d{1,2}\s*[/\-.]\s*\d{1,4}")
_TIME = re.compile(r"\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?")
_AMOUNT = re.compile(r"(?<![\d,*])([+-]?)\s*(\d{1,3}(?:,\d{3})+|\d{4,})(?![\d,*])\s*([+-]?)")


@dataclass(frozen=True, slots=True)
class Deposit:
    amount_rial: int
    source: str  # "sign" (a + marked amount), "word" (a deposit word on its line), "forwarder"


@dataclass(frozen=True, slots=True)
class Envelope:
    """One delivery from a forwarder app, from JSON or form fields."""

    text: str
    key: str | None
    message_id: str | None
    is_test: bool
    is_deposit: bool | None
    amount_rial: int | None


@cache
def _words() -> dict[str, tuple[str, ...]]:
    data = json.loads(WORDS_FILE.read_text(encoding="utf-8"))
    return {key: tuple(value) for key, value in data.items() if isinstance(value, list)}


def normalize(text: str) -> str:
    cleaned = (text or "")[:TEXT_MAX].replace("\r\n", "\n").replace("\r", "\n")
    cleaned = to_latin_digits(cleaned).translate(_LETTERS)
    cleaned = cleaned.replace("\u066c", ",").replace("\u060c", ",")  # Arabic thousands separator, comma
    cleaned = _FOOTER.sub("", cleaned)
    return "\n".join(" ".join(line.split()) for line in cleaned.split("\n") if line.strip())


def _has(line: str, group: str) -> bool:
    return any(word in line for word in _words()[group])


def parse(text: str) -> Deposit | None:
    """The deposited amount in Rial, or None when the message is not clearly one deposit."""
    signed: set[int] = set()
    worded: set[int] = set()
    withdrawn = deposited = False
    for raw in normalize(text).split("\n"):
        line = _TIME.sub(" ", _DATE.sub(" ", raw))
        if _has(line, "balance"):
            continue  # "balance: 12,000,000" is never the deposit
        deposit_line = _has(line, "deposit") and not _has(line, "withdrawal")
        deposited = deposited or deposit_line
        if _has(line, "withdrawal") and not deposit_line:
            withdrawn = True
        # The amount sits on the deposit line, or on its own "amount:" line next to it.
        amount_line = deposit_line or (_has(line, "amount") and not _has(line, "withdrawal"))
        toman = _has(line, "toman")
        for before, digits, after in _AMOUNT.findall(line):
            amount = int(digits.replace(",", ""))
            if amount <= 0 or ("," not in digits and len(digits) > 10):
                continue  # a phone, card or account number, never a payment amount
            rial = amount * 10 if toman else amount
            if "-" in (before, after):
                withdrawn = True
            elif "+" in (before, after):
                signed.add(rial)
            elif amount_line and ("," in digits or not _has(line, "account")):
                worded.add(rial)
    if len(signed) == 1:
        return Deposit(signed.pop(), "sign")
    if signed or withdrawn or not deposited:
        return None
    if len(worded) == 1:
        return Deposit(worded.pop(), "word")
    return None


def _first(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is not None and not isinstance(value, (dict, list)) and str(value).strip():
            return str(value).strip()
    return None


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower() if value is not None else ""
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    return None


def envelope(data: dict[str, Any]) -> Envelope | None:
    """Read the fields forwarder apps use; None when there is no message text."""
    text = _first(data, "text", "message", "msg", "content", "body")
    if text is None:
        return None
    amount_rial = None
    raw = _first(data, "amount")
    if raw is not None and raw.replace(",", "").isdigit():
        unit = (_first(data, "unit") or "rial").lower()
        if unit in {"rial", "toman"}:
            amount_rial = int(raw.replace(",", "")) * (10 if unit == "toman" else 1)
    message_id = _first(data, "id", "message_id")
    return Envelope(
        text=text[:TEXT_MAX],
        key=_first(data, "key", "api_key", "secret"),
        message_id=re.sub(r"[^A-Za-z0-9_.:-]", "", message_id)[:128] or None if message_id else None,
        is_test=_flag(data.get("test")) is True,
        is_deposit=_flag(data.get("is_deposit")),
        amount_rial=amount_rial or None,
    )


def deposit_of(item: Envelope) -> Deposit | None:
    """What the SMS says arrived; the forwarder's own reading counts only for a declared deposit."""
    found = parse(item.text)
    if found is None and item.is_deposit is True and item.amount_rial:
        return Deposit(item.amount_rial, "forwarder")
    return found


def fingerprint(shop_id: int, item: Envelope) -> str:
    """One bank message, however many times the forwarder delivers it (time and balance make
    two real deposits read differently)."""
    digest = hashlib.sha256(f"{shop_id}\n{normalize(item.text)}".encode()).hexdigest()
    return f"sms:{digest[:48]}"


# --- unique amounts ---------------------------------------------------------------------------


async def _lock(session: AsyncSession, shop_id: int) -> None:
    await session.execute(
        sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"card-amount:{shop_id}"}
    )


async def reserve_amount(
    session: AsyncSession, shop_id: int, amount: int, high: int, *, tail: bool
) -> int | None:
    """An amount no other open card payment of the shop asks for: `amount` itself when free (and
    no random tail is wanted), else `amount` plus a random few Toman. None when all are taken.

    The lock is held until the caller commits, so two payments started together cannot both
    take the same amount."""
    await _lock(session, shop_id)
    taken = set(
        await session.scalars(
            select(Payment.amount).where(
                Payment.shop_id == shop_id,
                Payment.provider == "card",
                Payment.status.in_([PaymentStatus.CREATED, *OPEN]),
                Payment.amount.between(amount, amount + TAIL_MAX),
            )
        )
    )
    if not tail and amount not in taken:
        return amount
    free = [
        amount + extra
        for extra in range(1, TAIL_MAX + 1)
        if amount + extra <= high and amount + extra not in taken
    ]
    if not free:
        return None if amount in taken or amount > high else amount
    return secrets.choice(free)


# --- matching -----------------------------------------------------------------------------------


@dataclass(slots=True)
class Matched:
    outcome: str  # approved | duplicate | unmatched | ambiguous | not_deposit
    deposit: Deposit | None = None
    payment_id: int | None = None
    candidates: int = 0


async def match(session: AsyncSession, shop_id: int, item: Envelope) -> Matched:
    """Approve the one open card payment the deposit SMS pays for. The caller commits."""
    from paurubot.payments import engine

    deposit = deposit_of(item)
    if deposit is None:
        return Matched("not_deposit")
    if deposit.amount_rial % 10:
        return Matched("unmatched", deposit)  # Toman payments always end in a zero Rial digit
    await _lock(session, shop_id)  # one SMS at a time per shop, and never beside a reservation
    tx = fingerprint(shop_id, item)
    seen = await session.scalar(
        select(Payment.id).where(Payment.provider == "card", Payment.provider_tx_id == tx).limit(1)
    )
    if seen is not None:
        return Matched("duplicate", deposit, seen)
    rows = list(
        await session.scalars(
            select(Payment)
            .where(
                Payment.shop_id == shop_id,
                Payment.provider == "card",
                Payment.status.in_(OPEN),
                Payment.amount == deposit.amount_rial // 10,
            )
            .order_by(Payment.id)
            .limit(2)
            .with_for_update()
        )
    )
    if len(rows) != 1:
        return Matched("ambiguous" if rows else "unmatched", deposit, None, len(rows))
    payment = rows[0]
    had_receipt = payment.status == PaymentStatus.VERIFYING
    payment.review_note = "auto-approved: bank sms"
    payment.meta = {
        **(payment.meta or {}),
        "bank_sms": {"at": utcnow().isoformat(), "source": deposit.source, "message_id": item.message_id},
    }
    await session.flush()
    if not await engine.mark_paid(session, payment.id, provider_tx_id=tx):
        return Matched("unmatched", deposit, None, 1)
    if had_receipt:
        from paurubot.notifications import review_sync

        await review_sync.announce(session, payment.id, shop_id, "approved", "bank SMS")
    return Matched("approved", deposit, payment.id, 1)
