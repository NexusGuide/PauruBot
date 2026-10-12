"""Owner-facing reseller ranking from sales and support complaints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Customer, Order, OrderStatus, ResellerProfile, Shop, Ticket

REVENUE_UNIT = 100_000
SALES_WEIGHT = 10
COMPLAINT_WEIGHT = 20


@dataclass(frozen=True, slots=True)
class ResellerRank:
    rank: int
    profile_id: int
    name: str
    score: int
    sales: int
    revenue: int
    complaints: int


def score(sales: int, revenue: int, complaints: int) -> int:
    """A transparent score: ten points per sale, one per 100k revenue, minus complaints."""
    return max(0, sales * SALES_WEIGHT + revenue // REVENUE_UNIT - complaints * COMPLAINT_WEIGHT)


async def rankings(session: AsyncSession, shop_id: int) -> list[ResellerRank]:
    profiles = list(
        await session.scalars(
            select(ResellerProfile)
            .where(ResellerProfile.shop_id == shop_id, ResellerProfile.is_active.is_(True))
            .order_by(ResellerProfile.id)
        )
    )
    result: list[tuple[ResellerProfile, str, int, int, int]] = []
    for profile in profiles:
        customer = await session.get(Customer, profile.customer_id)
        if customer is None:
            continue
        child_ids = list(
            await session.scalars(select(Shop.id).where(Shop.reseller_profile_id == profile.id))
        )
        own_sales = (Order.shop_id == shop_id) & (Order.customer_id == profile.customer_id)
        sales_conditions: list[Any] = [
            Order.status == OrderStatus.COMPLETED,
            Order.amount > 0,
            or_(own_sales, Order.shop_id.in_(child_ids)) if child_ids else own_sales,
        ]
        sales, revenue = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(*sales_conditions)
            )
        ).one()
        own_tickets = (Ticket.shop_id == shop_id) & (Ticket.customer_id == profile.customer_id)
        ticket_scope: Any = (
            or_(own_tickets, Ticket.shop_id.in_(child_ids)) if child_ids else own_tickets
        )
        complaints = int(
            await session.scalar(select(func.count(Ticket.id)).where(ticket_scope)) or 0
        )
        result.append((profile, customer.display_name, int(sales or 0), int(revenue or 0), complaints))

    result.sort(key=lambda row: (-score(row[2], row[3], row[4]), row[4], -row[2], row[0].id))
    return [
        ResellerRank(
            rank=index,
            profile_id=profile.id,
            name=name,
            score=score(sales, revenue, complaints),
            sales=sales,
            revenue=revenue,
            complaints=complaints,
        )
        for index, (profile, name, sales, revenue, complaints) in enumerate(result, 1)
    ]
