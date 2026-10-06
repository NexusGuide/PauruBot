"""Excel exports of business data (FIN-05) and the top-customers report (FIN-06)."""

from __future__ import annotations

import io
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import (
    Customer,
    Order,
    OrderStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Plan,
    Service,
)

MAX_ROWS = 200_000
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe(value: Any) -> Any:
    """Keep spreadsheet apps from running text as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (list, dict)):
        return ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    if hasattr(value, "value") and not isinstance(value, (int, float, str)):
        return str(value.value)  # enums
    return value


async def _rows(session: AsyncSession, stmt: Any, batch: int = 2000) -> AsyncIterator[Any]:
    result = await session.stream(stmt.execution_options(yield_per=batch))
    count = 0
    async for row in result:
        yield row
        count += 1
        if count >= MAX_ROWS:
            break


def _workbook(title: str, header: Sequence[str]) -> tuple[Workbook, Any]:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title[:31])
    bold = Font(bold=True)
    cells = []
    for name in header:
        cell = WriteOnlyCell(ws, value=name)
        cell.font = bold
        cells.append(cell)
    ws.append(cells)
    return wb, ws


def _bytes(wb: Workbook) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


SHEETS: dict[str, tuple[str, ...]] = {
    "orders": (
        "id",
        "created_at",
        "completed_at",
        "customer_id",
        "customer",
        "kind",
        "status",
        "plan",
        "base_price",
        "reseller_discount",
        "code_discount",
        "amount",
        "source",
    ),
    "payments": (
        "id",
        "created_at",
        "paid_at",
        "customer_id",
        "customer",
        "provider",
        "purpose",
        "status",
        "amount",
        "provider_tx_id",
        "order_id",
    ),
    "customers": (
        "id",
        "telegram_id",
        "username",
        "first_name",
        "phone",
        "created_at",
        "last_seen_at",
        "is_banned",
        "tags",
        "risk",
        "color",
    ),
    "services": (
        "id",
        "customer_id",
        "customer",
        "panel_username",
        "label",
        "status",
        "plan",
        "used_bytes",
        "data_limit",
        "expire_at",
        "created_at",
    ),
}


async def build(
    session: AsyncSession,
    kind: str,
    shop_id: int,
    *,
    start: datetime | None,
    end: datetime | None,
    customer_ids: list[int] | None,
) -> bytes:
    """One sheet of `kind` for the shop, optionally limited to a date range and to some customers."""
    header = SHEETS[kind]
    wb, ws = _workbook(kind, header)
    if kind == "orders":
        order_stmt = (
            select(Order, Customer, Plan.title)
            .join(Customer, Customer.id == Order.customer_id)
            .outerjoin(Plan, Plan.id == Order.plan_id)
            .where(Order.shop_id == shop_id)
            .order_by(Order.id)
        )
        if start:
            order_stmt = order_stmt.where(Order.created_at >= start)
        if end:
            order_stmt = order_stmt.where(Order.created_at < end)
        if customer_ids is not None:
            order_stmt = order_stmt.where(Order.customer_id.in_(customer_ids))
        async for order, customer, plan in _rows(session, order_stmt):
            ws.append(
                [
                    safe(v)
                    for v in (
                        order.id,
                        order.created_at,
                        order.completed_at,
                        customer.id,
                        customer.display_name,
                        order.kind,
                        order.status,
                        plan or "",
                        order.base_price,
                        order.reseller_discount,
                        order.code_discount,
                        order.amount,
                        order.source,
                    )
                ]
            )
    elif kind == "payments":
        payment_stmt = (
            select(Payment, Customer)
            .join(Customer, Customer.id == Payment.customer_id)
            .where(Payment.shop_id == shop_id)
            .order_by(Payment.id)
        )
        if start:
            payment_stmt = payment_stmt.where(Payment.created_at >= start)
        if end:
            payment_stmt = payment_stmt.where(Payment.created_at < end)
        if customer_ids is not None:
            payment_stmt = payment_stmt.where(Payment.customer_id.in_(customer_ids))
        async for payment, customer in _rows(session, payment_stmt):
            ws.append(
                [
                    safe(v)
                    for v in (
                        payment.id,
                        payment.created_at,
                        payment.paid_at,
                        customer.id,
                        customer.display_name,
                        payment.provider,
                        payment.purpose,
                        payment.status,
                        payment.amount,
                        payment.provider_tx_id or "",
                        payment.order_id or "",
                    )
                ]
            )
    elif kind == "customers":
        customer_stmt = select(Customer).where(Customer.shop_id == shop_id).order_by(Customer.id)
        if start:
            customer_stmt = customer_stmt.where(Customer.created_at >= start)
        if end:
            customer_stmt = customer_stmt.where(Customer.created_at < end)
        async for (customer,) in _rows(session, customer_stmt):
            ws.append(
                [
                    safe(v)
                    for v in (
                        customer.id,
                        customer.telegram_id,
                        customer.username or "",
                        customer.first_name or "",
                        customer.phone or "",
                        customer.created_at,
                        customer.last_seen_at,
                        customer.is_banned,
                        customer.tags or [],
                        customer.risk,
                        customer.color or "",
                    )
                ]
            )
    elif kind == "services":
        service_stmt = (
            select(Service, Customer, Plan.title)
            .join(Customer, Customer.id == Service.customer_id)
            .outerjoin(Plan, Plan.id == Service.plan_id)
            .where(Service.shop_id == shop_id)
            .order_by(Service.id)
        )
        if start:
            service_stmt = service_stmt.where(Service.created_at >= start)
        if end:
            service_stmt = service_stmt.where(Service.created_at < end)
        if customer_ids is not None:
            service_stmt = service_stmt.where(Service.customer_id.in_(customer_ids))
        async for service, customer, plan in _rows(session, service_stmt):
            ws.append(
                [
                    safe(v)
                    for v in (
                        service.id,
                        customer.id,
                        customer.display_name,
                        service.panel_username,
                        service.label or "",
                        service.status,
                        plan or "",
                        service.used_bytes,
                        service.data_limit,
                        service.expire_at,
                        service.created_at,
                    )
                ]
            )
    else:
        raise KeyError(kind)
    return _bytes(wb)


async def top_customers(
    session: AsyncSession,
    shop_id: int,
    *,
    start: datetime,
    end: datetime,
    limit: int = 20,
    customer_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Customers ranked by what they spent on completed orders in the period, with top-ups beside it."""
    spent = (
        select(
            Order.customer_id.label("customer_id"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(Order.amount), 0).label("spent"),
            func.max(Order.completed_at).label("last_order"),
        )
        .where(
            Order.shop_id == shop_id,
            Order.status == OrderStatus.COMPLETED,
            Order.completed_at >= start,
            Order.completed_at < end,
        )
        .group_by(Order.customer_id)
    )
    if customer_ids is not None:
        spent = spent.where(Order.customer_id.in_(customer_ids))
    spent_sq = spent.subquery()
    topups = (
        select(
            Payment.customer_id.label("customer_id"),
            func.coalesce(func.sum(Payment.amount), 0).label("topped_up"),
        )
        .where(
            Payment.shop_id == shop_id,
            Payment.status == PaymentStatus.PAID,
            Payment.purpose == PaymentPurpose.TOPUP,
            Payment.paid_at >= start,
            Payment.paid_at < end,
        )
        .group_by(Payment.customer_id)
        .subquery()
    )
    stmt = (
        select(Customer, spent_sq.c.orders, spent_sq.c.spent, spent_sq.c.last_order, topups.c.topped_up)
        .join(spent_sq, spent_sq.c.customer_id == Customer.id)
        .outerjoin(topups, topups.c.customer_id == Customer.id)
        .order_by(spent_sq.c.spent.desc(), spent_sq.c.orders.desc())
        .limit(max(1, min(limit, 100)))
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "customer_id": customer.id,
            "customer": customer.display_name,
            "telegram_id": customer.telegram_id,
            "is_reseller": customer.is_reseller,
            "orders": int(orders),
            "spent": int(spent_total),
            "topped_up": int(topped or 0),
            "last_order": last.isoformat() if last else None,
        }
        for customer, orders, spent_total, last, topped in rows
    ]
