"""The reseller's own selling desk in the web panel (TEN-15): sell to an end customer with a label,
services about to run out with one-click renewal, all paid from its wallet."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from paurubot.api.admin_common import reseller_customer_id
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import catalog, orders, pricing, reseller_desk
from paurubot.db.models import Customer
from paurubot.ledger.accounts import wallet_balance

router = APIRouter(prefix="/api/admin/desk", tags=["admin"])
Seller = Annotated[StaffContext, Depends(require("services.create"))]


async def _reseller(db: DB, ctx: StaffContext) -> Customer:
    """Only a reseller's own sign-in has a desk: shop staff sell from the bot or create services."""
    customer_id = await reseller_customer_id(db, ctx)
    if customer_id is None:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="not_found")
    await db.refresh(customer, ["reseller_profile"])
    return customer


@router.get("")
async def desk(ctx: Seller, db: DB) -> dict[str, Any]:
    customer = await _reseller(db, ctx)
    figures = await reseller_desk.figures(session=db, customer=customer)
    plans = []
    for plan in await catalog.plans(db, ctx.shop_id, customer):
        if plan.delivery == "stock":
            continue
        quote = await pricing.plan_quote(db, customer, plan.id, plan.price)
        plans.append(
            {
                "id": plan.id,
                "title": plan.title,
                "days": plan.days,
                "data_gb": plan.data_gb,
                "base_price": plan.price,
                "price": quote.amount,
            }
        )
    return {
        "balance": await wallet_balance(db, ctx.shop_id, customer.id),
        "figures": {
            "active": figures.active,
            "expiring": figures.expiring,
            "month_orders": figures.month_orders,
            "month_amount": figures.month_amount,
        },
        "plans": plans,
    }


class SellBody(BaseModel):
    plan_id: int
    label: str | None = Field(default=None, max_length=48)


@router.post("/sell")
async def sell(body: SellBody, ctx: Seller, db: DB) -> dict[str, Any]:
    customer = await _reseller(db, ctx)
    try:
        async with db.begin_nested():
            order = await reseller_desk.sell(db, customer, body.plan_id, body.label, source="reseller_web")
    except orders.OrderError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.sell",
        entity="order",
        entity_id=order.id,
        after={"plan_id": body.plan_id, "amount": order.amount},
    )
    return {"order_id": order.id, "balance": await wallet_balance(db, ctx.shop_id, customer.id)}


@router.get("/expiring")
async def expiring(ctx: Seller, db: DB, days: int = Query(3, ge=1, le=60)) -> dict[str, Any]:
    customer = await _reseller(db, ctx)
    return {"items": await reseller_desk.expiring_rows(db, customer, days)}


class RenewBody(BaseModel):
    service_ids: list[int] = Field(min_length=1, max_length=reseller_desk.RENEW_LIMIT)


@router.post("/renew")
async def renew(body: RenewBody, ctx: Seller, db: DB) -> dict[str, Any]:
    customer = await _reseller(db, ctx)
    services = await reseller_desk.owned_services(db, customer, body.service_ids)
    result = await reseller_desk.renew_many(db, customer, services)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.renew_many",
        entity="service",
        after={"renewed": len(result.renewed), "failed": len(result.failed)},
    )
    return {
        "renewed": [s.id for s in result.renewed],
        "failed": [{"id": s.id, "error": key} for s, key in result.failed],
        "balance": await wallet_balance(db, ctx.shop_id, customer.id),
    }
