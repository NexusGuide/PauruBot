"""Profit reports for a reseller's own sales bot (TEN-20)."""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from paurubot.core.reports import day_bounds
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderStatus
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class ProfitPeriod:
    key: str
    start: datetime
    end: datetime
    sales: int
    revenue: int
    wholesale: int
    profit: int


def period_bounds(key: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Calendar-day-aligned rolling periods in the workspace timezone."""
    if key not in {"day", "week", "month"}:
        raise ValueError(key)
    start, end = day_bounds(now or utcnow())
    days = {"day": 1, "week": 7, "month": 30}[key]
    return start - timedelta(days=days - 1), end


async def _orders(
    session: AsyncSession, shop_id: int, start: datetime, end: datetime
) -> list[tuple[Order, Customer]]:
    rows = await session.execute(
        select(Order, Customer)
        .join(Customer, Customer.id == Order.customer_id)
        .where(
            Order.shop_id == shop_id,
            Order.status == OrderStatus.COMPLETED,
            Order.amount > 0,
            Order.completed_at >= start,
            Order.completed_at < end,
        )
        .order_by(Order.completed_at, Order.id)
    )
    return list(rows.all())


async def summary(session: AsyncSession, shop_id: int, key: str) -> ProfitPeriod:
    start, end = period_bounds(key)
    rows = await _orders(session, shop_id, start, end)
    revenue = 0
    wholesale = 0
    for order, _customer in rows:
        revenue += int(order.amount or 0)
        wholesale += int((order.params or {}).get("wholesale") or 0)
    return ProfitPeriod(key, start, end, len(rows), revenue, wholesale, revenue - wholesale)


async def workbook(session: AsyncSession, shop_id: int, key: str) -> bytes:
    """Build a safe two-sheet XLSX with summary and completed reseller orders."""
    period = await summary(session, shop_id, key)
    rows = await _orders(session, shop_id, period.start, period.end)
    wb = Workbook(write_only=True)
    summary_ws = wb.create_sheet("summary")
    orders_ws = wb.create_sheet("orders")
    bold = Font(bold=True)

    def header(ws: Any, values: list[str]) -> None:
        cells = []
        for value in values:
            cell = WriteOnlyCell(ws, value=value)
            cell.font = bold
            cells.append(cell)
        ws.append(cells)

    header(summary_ws, ["period", "start", "end", "sales", "revenue", "wholesale", "profit"])
    summary_ws.append(
        [
            period.key,
            period.start.replace(tzinfo=None),
            period.end.replace(tzinfo=None),
            period.sales,
            period.revenue,
            period.wholesale,
            period.profit,
        ]
    )
    header(
        orders_ws,
        ["order_id", "completed_at", "customer_id", "customer", "kind", "revenue", "wholesale", "profit"],
    )
    for order, customer in rows:
        revenue = int(order.amount or 0)
        cost = int((order.params or {}).get("wholesale") or 0)
        completed = order.completed_at.replace(tzinfo=None) if order.completed_at else None
        orders_ws.append(
            [
                order.id,
                completed,
                customer.id,
                customer.display_name,
                str(order.kind),
                revenue,
                cost,
                revenue - cost,
            ]
        )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
