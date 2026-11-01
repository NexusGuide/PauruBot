"""Match confirmed on-chain transfers to open crypto payments (PAY-11, SEC-F10).

Chain APIs return newest transfers first, a page at a time. A busy or dust-spammed address can
hold more transfers in the 24-hour window than one page, so a scan pages until it reaches the
window start. Work per scan is bounded (`MAX_PAGES` per address), and anything not reached is
kept as a resumable gap:

* `hw` (high water) is the newest chain time already read. The next scan reads the live edge
  only down to `hw - RESCAN`, which still covers indexer lag and the payment clock skew.
* A sweep that runs out of budget, or fails part-way, stores where it stopped. Later scans read
  the live edge first, then continue the oldest unread gaps.
* Transfers that matched several payments are retried from the stored state for the whole
  window. That needs no chain re-reads, and a candidate settling later can still resolve them.

This state lives in Redis and is only a cursor. Credit safety stays in the database (unique
provider tx and `mark_paid`). Missing or corrupt state means one bounded re-read of the window.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments.chains import MATCH_WINDOW, NETWORKS, ChainError, Client, Transfer

log = logging.getLogger(__name__)

LATE_WINDOW = MATCH_WINDOW
CLOCK_SKEW = 120
# Seconds below the high-water mark that every scan re-reads. Both APIs list confirmed transfers in
# chain order (TRON solidified blocks, TON account lt), so this only absorbs indexer lag and the
# payment clock skew. Keep it small: the live edge must fit the page budget for gaps to progress.
RESCAN = 300
MAX_PAGES = 10  # page requests per address per scan
MAX_GAPS = 8
MAX_RETRY = 50
STATE_TTL = int(LATE_WINDOW.total_seconds()) * 2


def _match(transfer: Transfer, payments: list[Payment], chain: str) -> list[Payment]:
    found = []
    for payment in payments:
        if transfer.timestamp < int(payment.created_at.timestamp()) - CLOCK_SKEW:
            continue
        exact = transfer.amount == payment.provider_amount
        memo = (
            chain == "ton"
            and transfer.memo.strip() == f"P{payment.id}"
            and transfer.amount >= (payment.provider_amount or 0)
        )
        if exact or memo:
            found.append(payment)
    return found


# ---------------------------------------------------------------- resumable scan state


def state_key(shop_id: int, network: str, address: str) -> str:
    return f"crypto:scan:{shop_id}:{network}:{address}"


def _int(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _cursor(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    if set(value) == {"max_ms"} and _int(value["max_ms"]) is not None:
        return {"max_ms": value["max_ms"]}
    if set(value) == {"lt", "hash"} and all(isinstance(value[k], str) and value[k] for k in value):
        return {"lt": value["lt"], "hash": value["hash"]}
    return None


def _transfer(value: Any) -> Transfer | None:
    if not isinstance(value, dict):
        return None
    tx_id, memo = value.get("tx"), value.get("memo", "")
    amount, ts = _int(value.get("amount")), _int(value.get("ts"))
    if not isinstance(tx_id, str) or not tx_id or amount is None or ts is None:
        return None
    return Transfer(tx_id, amount, ts, memo if isinstance(memo, str) else "")


@dataclass(slots=True)
class Gap:
    """Unread transfers newer than `until` and at or before the `cursor` position (time `at`)."""

    until: int
    at: int
    cursor: dict[str, Any]


@dataclass(slots=True)
class ScanState:
    hw: int | None = None
    gaps: list[Gap] = field(default_factory=list)  # newest first
    retry: list[Transfer] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: Any) -> ScanState:
        """Untrusted Redis content: any malformed part is dropped, never guessed."""
        try:
            data = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        raw_gaps, raw_retry = data.get("gaps"), data.get("retry")
        gaps = []
        for item in raw_gaps if isinstance(raw_gaps, list) else []:
            if not isinstance(item, dict):
                continue
            until, at, cursor = _int(item.get("until")), _int(item.get("at")), _cursor(item.get("cursor"))
            if until is not None and at is not None and cursor is not None:
                gaps.append(Gap(until, at, cursor))
        retry = (
            [t for item in raw_retry if (t := _transfer(item)) is not None]
            if isinstance(raw_retry, list)
            else []
        )
        return cls(_int(data.get("hw")), gaps[:MAX_GAPS], retry[:MAX_RETRY])

    def dump(self) -> str:
        return json.dumps(
            {
                "hw": self.hw,
                "gaps": [{"until": g.until, "at": g.at, "cursor": g.cursor} for g in self.gaps],
                "retry": [
                    {"tx": t.tx_id, "amount": t.amount, "ts": t.timestamp, "memo": t.memo} for t in self.retry
                ],
            },
            separators=(",", ":"),
        )


async def _load(key: str) -> ScanState:
    try:
        return ScanState.parse(await kv.get_redis().get(key))
    except Exception as exc:  # noqa: BLE001 - Redis down: degrade to a bounded full-window read
        log.warning("crypto scan state unavailable: %s", exc.__class__.__name__)
        return ScanState()


async def _save(key: str, state: ScanState) -> None:
    try:
        await kv.get_redis().set(key, state.dump(), ex=STATE_TTL)
    except Exception as exc:  # noqa: BLE001 - next scan re-reads; credits are idempotent in the DB
        log.warning("crypto scan state not saved: %s", exc.__class__.__name__)


# ---------------------------------------------------------------- bounded sweeps


@dataclass(slots=True)
class Sweep:
    pages: int = 0
    complete: bool = False
    failed: bool = False
    newest: int | None = None
    at: int | None = None  # time of `resume`
    resume: dict[str, Any] | None = None


async def _sweep(
    client: Client,
    network: str,
    address: str,
    key: str,
    since: int,
    cursor: dict[str, Any] | None,
    budget: int,
    found: dict[str, Transfer],
) -> Sweep:
    """Read pages newest-first from `cursor` (or the newest transfer) down to `since`."""
    result = Sweep()
    while result.pages < budget:
        try:
            page = await client.page(network, address, since, key, cursor)
        except ChainError as exc:
            log.warning("crypto scan failed: %s", exc)
            result.failed = True
            return result
        result.pages += 1
        for transfer in page.transfers:
            if transfer.timestamp >= since:
                found.setdefault(transfer.tx_id, transfer)
        if page.newest is not None and result.newest is None:
            result.newest = page.newest
        if page.next is None:
            result.complete = True
            return result
        cursor = page.next
        result.at, result.resume = page.oldest, page.resume
    return result


async def _collect(
    client: Client, network: str, address: str, key: str, window: int, state: ScanState
) -> tuple[dict[str, Transfer], ScanState] | None:
    """Read the live edge, then unread gaps, within `MAX_PAGES`. None if the API is unreachable."""
    found: dict[str, Transfer] = {}
    budget = MAX_PAGES
    if state.hw is not None and state.hw > int(utcnow().timestamp()) + CLOCK_SKEW:
        # A high-water mark in the future would hide every new transfer until the state expires.
        log.warning("crypto scan state for %s is ahead of the clock; re-reading the window", network)
        state = ScanState(None, [], state.retry)
    lower = window if state.hw is None else max(window, state.hw - RESCAN)
    top = await _sweep(client, network, address, key, lower, None, budget, found)
    if top.pages == 0:
        return None  # first request failed: keep the stored state for the next scan
    budget -= top.pages
    hw = max(filter(None, [state.hw, top.newest]), default=None)
    gaps = list(state.gaps)
    if not top.complete:
        if top.resume is not None and top.at is not None:
            gaps.insert(0, Gap(lower, top.at, top.resume))
        else:
            # Failed before any position was known: re-read this edge next time.
            hw = state.hw
    remaining: list[Gap] = []
    failed = top.failed
    for gap in gaps:
        until = max(gap.until, window)
        if gap.at < until:
            continue  # everything this gap held has left the payment window
        if budget <= 0 or failed:
            remaining.append(gap)
            continue
        part = await _sweep(client, network, address, key, until, gap.cursor, budget, found)
        budget -= part.pages
        failed = part.failed
        if not part.complete:
            if part.resume is not None and part.at is not None:
                remaining.append(Gap(gap.until, part.at, part.resume))
            else:
                remaining.append(gap)
    if len(remaining) > MAX_GAPS:
        log.error(
            "crypto scan backlog for %s exceeds %s gaps; dropping the oldest unread ranges",
            network,
            MAX_GAPS,
        )
        remaining = remaining[:MAX_GAPS]
    for transfer in state.retry:
        if transfer.timestamp >= window:
            found.setdefault(transfer.tx_id, transfer)
    return found, ScanState(hw, remaining, [])


async def scan(session: AsyncSession, client: Client | None = None) -> int:
    """One pass over every open crypto payment. Returns how many were marked paid. Commits."""
    client = client or Client()
    since = utcnow() - LATE_WINDOW
    rows = list(
        await session.scalars(
            select(Payment).where(
                Payment.provider.in_(list(NETWORKS)),
                Payment.status.in_([PaymentStatus.PENDING, PaymentStatus.EXPIRED]),
                Payment.created_at >= since,
            )
        )
    )
    groups: dict[tuple[int, str, str], list[Payment]] = defaultdict(list)
    for payment in rows:
        if payment.provider_ref:
            groups[(payment.shop_id, payment.provider, payment.provider_ref)].append(payment)
    paid = 0
    for (shop_id, network, address), payments in groups.items():
        cfg = await shop_settings.load(session, shop_id)
        if not address:
            continue
        key = str(
            cfg.get("toncenter_api_key" if NETWORKS[network]["chain"] == "ton" else "trongrid_api_key") or ""
        )
        window = int(min(p.created_at for p in payments).timestamp()) - CLOCK_SKEW
        storage = state_key(shop_id, network, address)
        collected = await _collect(client, network, address, key, window, await _load(storage))
        if collected is None:
            continue
        found, state = collected
        transfers = list(found.values())
        used = set()
        if transfers:
            used = set(
                await session.scalars(
                    select(Payment.provider_tx_id).where(
                        Payment.provider == network, Payment.provider_tx_id.in_([t.tx_id for t in transfers])
                    )
                )
            )
        open_payments = [p for p in payments if p.provider_ref == address]
        for transfer in sorted(transfers, key=lambda t: (t.timestamp, t.tx_id)):
            if transfer.tx_id in used:
                continue
            matches = _match(transfer, open_payments, NETWORKS[network]["chain"])
            if len(matches) != 1:
                if matches:
                    log.warning(
                        "transfer %s matches %s payments; left for review", transfer.tx_id, len(matches)
                    )
                    for candidate in matches:
                        candidate.meta = {
                            **(candidate.meta or {}),
                            "crypto_ambiguity": {
                                "tx_id": transfer.tx_id,
                                "payment_ids": [p.id for p in matches],
                            },
                        }
                    state.retry.append(transfer)
                continue
            payment = matches[0]
            open_payments.remove(payment)
            used.add(transfer.tx_id)
            if payment.status == PaymentStatus.PENDING:
                if await engine.mark_paid(
                    session, payment.id, provider_tx_id=transfer.tx_id, amount=payment.amount
                ):
                    paid += 1
            else:
                # Paid after the deadline: the rate may have moved, so staff decide.
                payment.status = PaymentStatus.VERIFYING
                payment.meta = {
                    **(payment.meta or {}),
                    "late_tx": transfer.tx_id,
                    "late_amount": transfer.amount,
                }
                payment.review_note = "paid after expiry"
        await session.commit()
        state.retry = sorted(state.retry, key=lambda t: t.timestamp, reverse=True)[:MAX_RETRY]
        # Only after the commit: a crash before this point simply re-reads the same range.
        await _save(storage, state)
    return paid
