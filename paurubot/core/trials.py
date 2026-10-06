"""Trial policy with generations, daily quota and race-free reservations (CAT-04).

A reservation is the unique (shop, customer, generation) row in `trial_grants`: two concurrent
requests cannot both get a trial. Raising `trial_generation` lets every customer try again.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderKind, OrderStatus, Panel, TrialGrant
from paurubot.worker import queue


class TrialError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


async def eligibility(session: AsyncSession, customer: Customer) -> str | None:
    """None when the customer may take a trial, else a locale key with the reason."""
    cfg = await shop_settings.load(session, customer.shop_id)
    if not cfg["trial_enabled"]:
        return "trial_disabled"
    if (cfg["trial_requires_phone"] or cfg["require_phone"]) and not customer.phone:
        return "trial_needs_phone"
    generation = int(cfg["trial_generation"] or 1)
    taken = await session.scalar(
        select(TrialGrant.id).where(
            TrialGrant.shop_id == customer.shop_id,
            TrialGrant.customer_id == customer.id,
            TrialGrant.generation == generation,
        )
    )
    if taken:
        return "trial_already_used"
    quota = int(cfg["trial_daily_quota"] or 0)
    if quota:
        since = utcnow() - timedelta(days=1)
        today = await session.scalar(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.shop_id == customer.shop_id, TrialGrant.created_at >= since
            )
        )
        if int(today or 0) >= quota:
            return "trial_quota_reached"
    return None


async def _trial_panel(session: AsyncSession, shop_id: int, panel_id: int | None) -> Panel | None:
    if panel_id:
        panel = await session.get(Panel, int(panel_id))
        if panel is not None and panel.shop_id == shop_id and panel.is_active:
            return panel
        return None
    return await session.scalar(
        select(Panel).where(Panel.shop_id == shop_id, Panel.is_active.is_(True)).order_by(Panel.id).limit(1)
    )


async def request(session: AsyncSession, customer: Customer) -> Order:
    """Reserve a trial and queue its provisioning as a zero-amount order."""
    reason = await eligibility(session, customer)
    if reason:
        raise TrialError(reason)
    cfg = await shop_settings.load(session, customer.shop_id)
    panel = await _trial_panel(session, customer.shop_id, cfg["trial_panel_id"])
    if panel is None:
        raise TrialError("trial_disabled")
    generation = int(cfg["trial_generation"] or 1)
    grant_id = await session.scalar(
        insert(TrialGrant)
        .values(
            shop_id=customer.shop_id,
            customer_id=customer.id,
            generation=generation,
            status="reserved",
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["shop_id", "customer_id", "generation"])
        .returning(TrialGrant.id)
    )
    if grant_id is None:
        raise TrialError("trial_already_used")
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.PAID,
        params={
            "panel_id": panel.id,
            "days": int(cfg["trial_days"] or 1),
            "gb": int(cfg["trial_gb"] or 1),
            "grant_id": grant_id,
            "label": "trial",
        },
        amount=0,
        paid_at=utcnow(),
    )
    session.add(order)
    await session.flush()
    await queue.enqueue(
        session,
        "provision",
        "order",
        {"order_id": order.id},
        shop_id=customer.shop_id,
        dedupe_key=f"order:{order.id}:provision",
        max_attempts=4,
    )
    customer.trial_generation = generation
    return order


async def release(session: AsyncSession, grant_id: int) -> None:
    """Give the reservation back after a failed provisioning so the customer can retry."""
    await session.execute(delete(TrialGrant).where(TrialGrant.id == grant_id))
