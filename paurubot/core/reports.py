"""The end-of-day summary sent to the reports channel or to staff (FIN-04, JOB-02)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.config import get_settings
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerAccountKind,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    PaymentStatus,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
    TrialGrant,
)


@dataclass(slots=True)
class DailySummary:
    orders: int
    revenue: int
    renewals: int
    new_customers: int
    trials: int
    active_services: int
    expiring_soon: int
    pending_receipts: int
    open_tickets: int
    panels_down: int
    wallet_total: int


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    """Midnight to midnight in the shop's time zone, as aware datetimes."""
    tz = get_settings().tz
    local = day.astimezone(tz)
    start = datetime.combine(local.date(), time.min, tzinfo=tz)
    return start, start + timedelta(days=1)


async def daily_summary(session: AsyncSession, shop_id: int, start: datetime, end: datetime) -> DailySummary:
    async def count(stmt) -> int:  # type: ignore[no-untyped-def]
        return int(await session.scalar(stmt) or 0)

    sold = (
        Order.shop_id == shop_id,
        Order.status == OrderStatus.COMPLETED,
        Order.completed_at >= start,
        Order.completed_at < end,
    )
    return DailySummary(
        orders=await count(select(func.count(Order.id)).where(*sold)),
        revenue=await count(select(func.coalesce(func.sum(Order.amount), 0)).where(*sold)),
        renewals=await count(select(func.count(Order.id)).where(*sold, Order.kind == OrderKind.RENEW)),
        new_customers=await count(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop_id, Customer.created_at >= start, Customer.created_at < end
            )
        ),
        trials=await count(
            select(func.count(TrialGrant.id)).where(
                TrialGrant.shop_id == shop_id, TrialGrant.created_at >= start, TrialGrant.created_at < end
            )
        ),
        active_services=await count(
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
            )
        ),
        expiring_soon=await count(
            select(func.count(Service.id)).where(
                Service.shop_id == shop_id,
                Service.status == ServiceStatus.ACTIVE,
                Service.expire_at >= end,
                Service.expire_at < end + timedelta(days=3),
            )
        ),
        pending_receipts=await count(
            select(func.count(Payment.id)).where(
                Payment.shop_id == shop_id, Payment.status == PaymentStatus.VERIFYING
            )
        ),
        open_tickets=await count(
            select(func.count(Ticket.id)).where(Ticket.shop_id == shop_id, Ticket.status == TicketStatus.OPEN)
        ),
        panels_down=await count(
            select(func.count(Panel.id)).where(
                Panel.shop_id == shop_id, Panel.is_active.is_(True), Panel.is_healthy.is_(False)
            )
        ),
        wallet_total=await count(
            select(func.coalesce(func.sum(LedgerAccount.balance), 0)).where(
                LedgerAccount.shop_id == shop_id, LedgerAccount.kind == LedgerAccountKind.WALLET
            )
        ),
    )


def as_params(summary: DailySummary) -> dict[str, int]:
    return asdict(summary)


# --- sales funnel (ADM-06) ----------------------------------------------------------------

FUNNEL_STEPS = ("joined", "phone", "trial", "first_purchase", "repeat")


async def funnel(
    session: AsyncSession, shop_id: int, start: datetime, end: datetime, customer_ids: list[int] | None = None
) -> dict[str, Any]:
    """Of the people who started the bot in the period: how many reached each step, ever."""

    cohort = select(Customer.id).where(
        Customer.shop_id == shop_id, Customer.created_at >= start, Customer.created_at < end
    )
    if customer_ids is not None:
        cohort = cohort.where(Customer.id.in_(customer_ids))
    ids = list(await session.scalars(cohort))
    if not ids:
        return {"steps": [{"key": k, "count": 0} for k in FUNNEL_STEPS], "days_to_first": None}
    paid = (
        select(
            Order.customer_id, func.count(Order.id).label("n"), func.min(Order.completed_at).label("first")
        )
        .where(
            Order.customer_id.in_(ids),
            Order.status == OrderStatus.COMPLETED,
            Order.amount > 0,
            Order.kind != OrderKind.TRIAL,
        )
        .group_by(Order.customer_id)
        .subquery()
    )
    buyers = (await session.execute(select(paid.c.customer_id, paid.c.n, paid.c.first))).all()
    joined_at = dict(
        (await session.execute(select(Customer.id, Customer.created_at).where(Customer.id.in_(ids)))).all()
    )
    phone = int(
        await session.scalar(
            select(func.count(Customer.id)).where(Customer.id.in_(ids), Customer.phone.is_not(None))
        )
        or 0
    )
    trial = int(
        await session.scalar(
            select(func.count(func.distinct(TrialGrant.customer_id))).where(
                TrialGrant.customer_id.in_(ids), TrialGrant.status == "issued"
            )
        )
        or 0
    )
    waits = sorted(
        (first - joined_at[cid]).total_seconds() / 86400
        for cid, _n, first in buyers
        if first and cid in joined_at
    )
    median = round(waits[len(waits) // 2], 1) if waits else None
    counts = {
        "joined": len(ids),
        "phone": phone,
        "trial": trial,
        "first_purchase": len(buyers),
        "repeat": sum(1 for _cid, n, _first in buyers if n >= 2),
    }
    return {"steps": [{"key": k, "count": counts[k]} for k in FUNNEL_STEPS], "days_to_first": median}
