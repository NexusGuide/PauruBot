"""Match confirmed on-chain transfers to open crypto payments (PAY-11)."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Payment, PaymentStatus
from paurubot.payments import engine
from paurubot.payments.chains import NETWORKS, ChainError, Client, Transfer
from paurubot.payments.providers.crypto import address_for

log = logging.getLogger(__name__)

LATE_WINDOW = timedelta(hours=24)
CLOCK_SKEW = 120


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
    groups: dict[tuple[int, str], list[Payment]] = defaultdict(list)
    for payment in rows:
        groups[(payment.shop_id, payment.provider)].append(payment)
    paid = 0
    for (shop_id, network), payments in groups.items():
        cfg = await shop_settings.load(session, shop_id)
        address = address_for(network, cfg)
        if not address:
            continue
        key = str(
            cfg.get("toncenter_api_key" if NETWORKS[network]["chain"] == "ton" else "trongrid_api_key") or ""
        )
        start = int(min(p.created_at for p in payments).timestamp()) - CLOCK_SKEW
        try:
            transfers = await client.incoming(network, address, start, key)
        except ChainError as exc:
            log.warning("crypto scan failed: %s", exc)
            continue
        if not transfers:
            continue
        used = set(
            await session.scalars(
                select(Payment.provider_tx_id).where(
                    Payment.provider == network, Payment.provider_tx_id.in_([t.tx_id for t in transfers])
                )
            )
        )
        open_payments = [p for p in payments if p.provider_ref == address]
        for transfer in sorted(transfers, key=lambda t: t.timestamp):
            if transfer.tx_id in used:
                continue
            matches = _match(transfer, open_payments, NETWORKS[network]["chain"])
            if len(matches) != 1:
                if matches:
                    log.warning(
                        "transfer %s matches %s payments; left for review", transfer.tx_id, len(matches)
                    )
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
    return paid
