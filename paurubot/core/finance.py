"""Money figures by period and an Excel export of orders, for staff in the bot (ADM-09)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Service,
    ServiceStatus,
)

PERIODS = (("today", 1), ("week", 7), ("month", 30))


@dataclass(slots=True)
class Period:
    key: str
    sales: int  # completed paid orders
    sales_amount: int
    topups: int
    topup_amount: int
    new_customers: int


async def summary(session: AsyncSession, shop_id: int) -> tuple[list[Period], int]:
    """Figures for today, 7 and 30 days, and the services active right now."""
    now = utcnow()
    out = []
    for key, days in PERIODS:
        since = now - timedelta(days=days)
        sales = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
                    Order.shop_id == shop_id,
                    Order.status == OrderStatus.COMPLETED,
                    Order.amount > 0,
                    Order.completed_at >= since,
                )
            )
        ).one()
        topups = (
            await session.execute(
                select(func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.shop_id == shop_id,
                    Payment.purpose == PaymentPurpose.TOPUP,
                    Payment.status == PaymentStatus.PAID,
                    Payment.paid_at >= since,
                )
            )
        ).one()
        joined = await session.scalar(
            select(func.count(Customer.id)).where(Customer.shop_id == shop_id, Customer.created_at >= since)
        )
        out.append(
            Period(key, int(sales[0]), int(sales[1]), int(topups[0]), int(topups[1]), int(joined or 0))
        )
    active = await session.scalar(
        select(func.count(Service.id)).where(
            Service.shop_id == shop_id, Service.status == ServiceStatus.ACTIVE
        )
    )
    return out, int(active or 0)


async def orders_xlsx(session: AsyncSession, shop_id: int, days: int) -> bytes:
    """Orders of the last `days` days as a spreadsheet (the web panel's export, FIN-05)."""
    from paurubot.core import exports

    return await exports.build(
        session, "orders", shop_id, start=utcnow() - timedelta(days=days), end=None, customer_ids=None
    )
