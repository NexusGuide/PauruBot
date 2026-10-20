"""Owned pending payments and side-effect-free saved checkout resume."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import select

from paurubot.api.webapp_auth import WebApp
from paurubot.db.base import utcnow
from paurubot.db.models import Order, Payment, PaymentStatus
from paurubot.payments import saved_checkout

router = APIRouter()
OPEN = (PaymentStatus.CREATED, PaymentStatus.PENDING, PaymentStatus.VERIFYING)
PAGE = 20


@router.get("/payments")
async def pending_payments(
    response: Response, ctx: WebApp, before: int | None = Query(None, ge=1)
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    stmt = (
        select(Payment, Order)
        .outerjoin(Order, Order.id == Payment.order_id)
        .where(
            Payment.shop_id == ctx.shop.id,
            Payment.customer_id == ctx.customer.id,
            Payment.status.in_(OPEN),
        )
    )
    if before is not None:
        stmt = stmt.where(Payment.id < before)
    rows = list((await ctx.session.execute(stmt.order_by(Payment.id.desc()).limit(PAGE + 1))).all())
    now = utcnow()
    return {
        "items": [
            {
                "id": payment.id,
                "provider": payment.provider,
                "amount": payment.amount,
                "status": payment.status.value,
                "order_id": order.id
                if order is not None and order.customer_id == ctx.customer.id and order.shop_id == ctx.shop.id
                else None,
                "expires_at": payment.expires_at.isoformat() if payment.expires_at else None,
                "action": saved_checkout.action(payment, now, order),
            }
            for payment, order in rows[:PAGE]
        ],
        "next_before": rows[PAGE - 1][0].id if len(rows) > PAGE else None,
    }


@router.get("/payments/{payment_id}/resume")
async def resume_payment(payment_id: int, response: Response, ctx: WebApp) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    payment = await ctx.session.scalar(
        select(Payment)
        .where(
            Payment.id == payment_id, Payment.shop_id == ctx.shop.id, Payment.customer_id == ctx.customer.id
        )
        .execution_options(populate_existing=True)
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="payment_not_found")
    order = (
        await ctx.session.get(Order, payment.order_id, populate_existing=True) if payment.order_id else None
    )
    view = saved_checkout.saved_payment(payment, utcnow(), order)
    if view is None:
        raise HTTPException(status_code=409, detail="payment_resume_unavailable")
    # No engine.start/verify, no _payment_view (Stars creates a link), no writes.
    return {"payment": view}
