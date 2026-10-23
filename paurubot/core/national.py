"""Explicit national-network switch. Never fall back after an uncertain panel API call."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import stock
from paurubot.db.models import Order, Panel, Plan, Service, StockItem


async def route(
    db: AsyncSession, plan: Plan, service: Service | None = None, *, locked: bool = False
) -> Plan | None:
    panel_id = service.panel_id if service is not None else plan.panel_id
    panel = (
        await db.get(Panel, panel_id, populate_existing=locked, with_for_update=locked) if panel_id else None
    )
    if panel is None or not panel.national_mode:
        return None
    target = await db.get(Plan, plan.national_stock_plan_id) if plan.national_stock_plan_id else None
    if (
        target is None
        or target.shop_id != plan.shop_id
        or target.delivery != "stock"
        or not target.is_active
        or not plan.panel_id
        or plan.panel_id != panel.id
    ):
        raise stock.StockError("plan_unavailable")
    return target


async def replacement(db: AsyncSession, service: Service) -> StockItem | None:
    return await db.scalar(
        select(StockItem)
        .join(Order, Order.id == StockItem.order_id)
        .where(
            Order.service_id == service.id,
            StockItem.customer_id == service.customer_id,
            StockItem.status == stock.SOLD,
        )
        .order_by(StockItem.id.desc())
        .limit(1)
    )


async def validate_checkout(db: AsyncSession, order: Order) -> None:
    """A switch after checkout requires a new quote, not silently changing the paid product."""
    if not order.plan_id:
        return
    plan = await db.get(Plan, order.plan_id, populate_existing=True, with_for_update=True)
    if plan is None or plan.delivery == "stock":
        return
    service = await db.get(Service, order.service_id) if order.service_id else None
    target = await route(db, plan, service, locked=True)
    frozen = (order.params or {}).get("stock_plan_id")
    if (target.id if target else None) != frozen:
        raise stock.StockError("plan_unavailable")
