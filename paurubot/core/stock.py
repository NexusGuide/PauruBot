"""Selling ready-made configs from a stock instead of creating them on a panel (CAT-05).

Staff paste configs (one per line, or blocks separated by a blank line) into a plan whose
delivery is "stock". Each purchase takes exactly one available item under a row lock, so two
buyers can never receive the same config. Payloads are encrypted at rest because they are
bearer credentials.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import Order, Plan, Shop, StockItem
from paurubot.security import secretbox

PURPOSE = "stock"
MAX_ITEM = 8000
MAX_BATCH = 2000
AVAILABLE, SOLD, DISABLED, RESERVED = "available", "sold", "disabled", "reserved"


class StockError(Exception):
    pass


def split(text: str) -> list[str]:
    """Blocks separated by a blank line; without blank lines, one item per line."""
    clean = text.replace("\r\n", "\n").strip()
    if not clean:
        return []
    blocks = [b.strip() for b in clean.split("\n\n")] if "\n\n" in clean else clean.split("\n")
    return [b.strip() for b in blocks if b.strip()]


def _digest(shop_id: int, payload: str) -> str:
    return hashlib.sha256(f"{shop_id}:{payload}".encode()).hexdigest()


@dataclass(slots=True)
class Added:
    added: int
    duplicates: int
    too_long: int


async def add(session: AsyncSession, plan: Plan, text: str, note: str | None = None) -> Added:
    from paurubot.core import manual_stock

    await session.scalar(select(Shop.id).where(Shop.id == plan.shop_id).with_for_update())
    items = split(text)
    if len(items) > MAX_BATCH:
        raise StockError("stock_too_many")
    used = await manual_stock.storage_used(session, plan.shop_id)
    seen = set(
        await session.scalars(
            select(StockItem.digest).where(
                StockItem.shop_id == plan.shop_id,
                StockItem.digest.in_([_digest(plan.shop_id, i) for i in items]),
            )
        )
    )
    added = duplicates = too_long = 0
    for payload in items:
        if len(payload) > MAX_ITEM:
            too_long += 1
            continue
        digest = _digest(plan.shop_id, payload)
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        encrypted = secretbox.encrypt(PURPOSE, payload)
        size = len(encrypted.encode())
        if used + size > manual_stock.STORAGE_LIMIT:
            raise StockError("stock_storage_full")
        used += size
        session.add(
            StockItem(
                shop_id=plan.shop_id,
                plan_id=plan.id,
                payload_enc=encrypted,
                content_bytes=size,
                digest=digest,
                note=(note or None) and note[:128],
                status=AVAILABLE,
            )
        )
        added += 1
    await session.flush()
    return Added(added, duplicates, too_long)


async def counts(session: AsyncSession, plan_ids: list[int]) -> dict[int, dict[str, int]]:
    out: dict[int, dict[str, int]] = {
        pid: {AVAILABLE: 0, RESERVED: 0, SOLD: 0, DISABLED: 0} for pid in plan_ids
    }
    if not plan_ids:
        return out
    rows = await session.execute(
        select(StockItem.plan_id, StockItem.status, func.count(StockItem.id))
        .where(StockItem.plan_id.in_(plan_ids))
        .group_by(StockItem.plan_id, StockItem.status)
    )
    for plan_id, status, n in rows.all():
        out.setdefault(plan_id, {AVAILABLE: 0, RESERVED: 0, SOLD: 0, DISABLED: 0})[status] = int(n)
    return out


def available_stmt(plan_id_column):  # type: ignore[no-untyped-def]
    """EXISTS clause: the plan still has at least one item to sell."""
    return (
        select(StockItem.id)
        .where(StockItem.plan_id == plan_id_column, StockItem.status == AVAILABLE)
        .exists()
    )


async def take(session: AsyncSession, order: Order, *, reserve: bool = False) -> StockItem | None:
    """Hand one available item to this order. Idempotent: a retry returns the same item."""
    # Lock the order too: concurrent retries of the SAME order cannot take two units.
    await session.scalar(select(Order.id).where(Order.id == order.id).with_for_update())
    existing = await session.scalar(select(StockItem).where(StockItem.order_id == order.id))
    if existing is not None:
        if existing.status == RESERVED and not reserve:
            existing.status = SOLD
            existing.sold_at = utcnow()
            await session.flush()
        return existing
    plan_id = (order.params or {}).get("stock_plan_id") or order.plan_id
    plan = await session.get(Plan, plan_id) if plan_id else None
    from paurubot.core.resale import catalog_owner

    if (
        plan is None
        or plan.shop_id != await session.scalar(select(catalog_owner(order.shop_id)))
        or plan.delivery != "stock"
    ):
        return None
    item = await session.scalar(
        select(StockItem)
        .where(StockItem.plan_id == plan.id, StockItem.shop_id == plan.shop_id, StockItem.status == AVAILABLE)
        .order_by(StockItem.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if item is None:
        return None
    item.status = RESERVED if reserve else SOLD
    item.order_id = order.id
    item.customer_id = order.customer_id
    item.sold_at = None if reserve else utcnow()
    await session.flush()
    return item


def payload(item: StockItem) -> str:
    from paurubot.core.manual_stock import display

    return display(raw_payload(item))


def raw_payload(item: StockItem) -> str:
    # Customer configs are many and never logged; keep them out of the log redaction list.
    return secretbox.decrypt(PURPOSE, item.payload_enc, redact_in_logs=False)


async def for_customer(
    session: AsyncSession, customer_id: int, limit: int = 50, offset: int = 0
) -> list[StockItem]:
    return list(
        await session.scalars(
            select(StockItem)
            .where(StockItem.customer_id == customer_id, StockItem.status == SOLD)
            .order_by(StockItem.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )
