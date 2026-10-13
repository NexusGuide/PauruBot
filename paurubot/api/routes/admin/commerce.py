"""Orders and payments, including card receipt review."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot.api.admin_common import (
    Page,
    apply_sort,
    owned,
    page_params,
    paginate,
    plain,
    reseller_scope,
    sort_params,
)
from paurubot.api.confirmations import ConfirmFields, challenge, guard
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.db.models import Order, OrderStatus, Payment, PaymentStatus
from paurubot.payments import engine as payments
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])

ORDER_FIELDS = [
    "id",
    "customer_id",
    "kind",
    "status",
    "plan_id",
    "service_id",
    "base_price",
    "reseller_discount",
    "code_discount",
    "amount",
    "source",
    "error",
    "created_at",
    "paid_at",
    "completed_at",
]
PAYMENT_FIELDS = [
    "id",
    "customer_id",
    "order_id",
    "purpose",
    "provider",
    "status",
    "amount",
    "currency",
    "provider_amount",
    "provider_currency",
    "provider_tx_id",
    "receipt_text",
    "duplicate_of_id",
    "review_note",
    "created_at",
    "paid_at",
    "fulfilled_at",
    "reviewed_at",
    "failure_reason",
]


def payment_view(payment: Payment) -> dict[str, Any]:
    data = plain(payment, PAYMENT_FIELDS)
    data["reviewable"] = payments.awaiting_review(payment)
    data["review_amount"] = payments.disputed_credit(payment)
    return data


ORDER_SORT = {"id": Order.id, "amount": Order.amount, "created_at": Order.created_at, "status": Order.status}
PAYMENT_SORT = {
    "id": Payment.id,
    "amount": Payment.amount,
    "created_at": Payment.created_at,
    "status": Payment.status,
}


@router.get("/orders")
async def list_orders(
    ctx: Annotated[StaffContext, Depends(require("orders.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    status: OrderStatus | None = None,
    customer_id: int | None = None,
    order_id: int | None = Query(None, alias="id"),
    sort: str | None = Depends(sort_params),
) -> dict[str, Any]:
    stmt = select(Order).where(Order.shop_id == ctx.shop_id)
    if order_id is not None:
        stmt = stmt.where(Order.id == order_id)
    scope = await reseller_scope(db, ctx)
    if scope is not None:
        stmt = stmt.where(Order.customer_id.in_(scope))
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if customer_id:
        stmt = stmt.where(Order.customer_id == customer_id)
    rows, total = await paginate(db, apply_sort(stmt, sort, ORDER_SORT, Order.id), page)
    items = []
    for order in rows:
        data = plain(order, ORDER_FIELDS)
        data["customer"] = order.customer.display_name
        data["plan"] = order.plan.title if order.plan else None
        items.append(data)
    return {"items": items, "total": total}


@router.get("/payments")
async def list_payments(
    ctx: Annotated[StaffContext, Depends(require("payments.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    status: PaymentStatus | None = None,
    provider: str | None = None,
    customer_id: int | None = None,
    payment_id: int | None = Query(None, alias="id"),
    sort: str | None = Depends(sort_params),
) -> dict[str, Any]:
    stmt = select(Payment).where(Payment.shop_id == ctx.shop_id)
    if payment_id is not None:
        stmt = stmt.where(Payment.id == payment_id)
    if status is not None:
        stmt = stmt.where(Payment.status == status)
    if provider:
        stmt = stmt.where(Payment.provider == provider)
    if customer_id:
        stmt = stmt.where(Payment.customer_id == customer_id)
    rows, total = await paginate(db, apply_sort(stmt, sort, PAYMENT_SORT, Payment.id), page)
    items = []
    for payment in rows:
        data = payment_view(payment)
        data["customer"] = payment.customer.display_name
        data["has_receipt"] = bool(payment.receipt_file_id)
        items.append(data)
    return {"items": items, "total": total}


@router.get("/payments/{payment_id}/receipt")
async def receipt_image(
    payment_id: int, ctx: Annotated[StaffContext, Depends(require("payments.read"))], db: DB
) -> Response:
    payment = await owned(db, Payment, payment_id, ctx)
    if not payment.receipt_file_id:
        raise HTTPException(status_code=404, detail="no_receipt")
    from paurubot.notifications.telegram import bot_for

    bot = await bot_for(payment.shop_id)
    if bot is None:
        raise HTTPException(status_code=502, detail="telegram_unavailable")
    try:
        data = await bot.download(payment.receipt_file_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="telegram_unavailable") from exc
    body = data.read() if data else b""
    return Response(body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/payments/{payment_id}/approve")
async def approve(
    payment_id: int, ctx: Annotated[StaffContext, Depends(require("payments.review"))], db: DB
) -> dict[str, Any]:
    payment = await owned(db, Payment, payment_id, ctx)
    if not payments.manually_reviewable(payment):
        raise HTTPException(status_code=400, detail="not_manual")
    if not await payments.approve_manual(db, payment.id, ctx.staff.id):
        raise HTTPException(status_code=409, detail="payment_already_reviewed")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="payment.approved",
        entity="payment",
        entity_id=payment.id,
    )
    await db.commit()
    result = await payments.fulfill_one(db, payment.id)
    await db.refresh(payment)
    return {"payment": payment_view(payment), "order_settled": bool(result and result.order_settled)}


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=200)


@router.post("/payments/{payment_id}/reject")
async def reject(
    payment_id: int,
    body: RejectBody,
    ctx: Annotated[StaffContext, Depends(require("payments.review"))],
    db: DB,
) -> dict[str, Any]:
    payment = await owned(db, Payment, payment_id, ctx)
    if not await payments.reject(db, payment.id, staff_id=ctx.staff.id, note=body.reason):
        raise HTTPException(status_code=409, detail="payment_already_reviewed")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="payment.rejected",
        entity="payment",
        entity_id=payment.id,
        after={"reason": body.reason},
    )
    await queue.enqueue(
        db,
        "notify",
        "payment_rejected",
        {"payment_id": payment.id, "reason": body.reason},
        shop_id=ctx.shop_id,
    )
    await db.refresh(payment)
    return {"payment": payment_view(payment)}


def _refundable(order: Order) -> bool:
    return order.status in {OrderStatus.COMPLETED, OrderStatus.FAILED} and order.amount > 0


async def _large_refund(db: DB, ctx: StaffContext, order: Order) -> bool:
    from paurubot.core import settings as shop_settings

    floor = await shop_settings.get(db, ctx.shop_id, "typed_confirm_refund_min")
    return floor is not None and order.amount >= int(floor)


@router.post("/orders/{order_id}/confirm-refund")
async def confirm_refund(
    order_id: int, ctx: Annotated[StaffContext, Depends(require("payments.refund"))], db: DB
) -> dict[str, Any]:
    """Step one of a large refund (F-22): type the amount to confirm."""
    order = await owned(db, Order, order_id, ctx)
    if not _refundable(order):
        raise HTTPException(status_code=409, detail="not_refundable")
    return {
        **await challenge(ctx, "order.refund", order.id, str(order.amount)),
        "required": await _large_refund(db, ctx, order),
    }


class RefundBody(ConfirmFields):
    pass


@router.post("/orders/{order_id}/refund")
async def refund_order(
    order_id: int,
    ctx: Annotated[StaffContext, Depends(require("payments.refund"))],
    db: DB,
    body: RefundBody | None = None,
) -> dict[str, Any]:
    """Return a completed or failed order's money to the wallet (NEW-04). The service is not touched."""
    order = await owned(db, Order, order_id, ctx)
    if not _refundable(order):
        raise HTTPException(status_code=409, detail="not_refundable")
    if await _large_refund(db, ctx, order):
        await guard(db, ctx, "order.refund", order.id, str(order.amount), body)
    from paurubot.ledger import wallet

    await wallet.refund(
        db, order.shop_id, order.customer_id, order.amount, order_id=order.id, actor=f"staff:{ctx.staff.id}"
    )
    order.status = OrderStatus.REFUNDED
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="order.refund",
        entity="order",
        entity_id=order.id,
        after={"amount": order.amount},
    )
    await queue.enqueue(
        db,
        "notify",
        "wallet_adjusted",
        {"customer_id": order.customer_id, "delta": order.amount},
        shop_id=ctx.shop_id,
    )
    return {"order": plain(order, ORDER_FIELDS)}


# --- settlements (PAY-03) ----------------------------------------------------------


class SettlementBody(BaseModel):
    provider: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_]+$")
    amount: int = Field(ge=1, le=10**12)
    reference: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=255)


@router.get("/settlements")
async def settlements_view(
    ctx: Annotated[StaffContext, Depends(require("reports.read"))], db: DB
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    from paurubot.core import settlements

    rows = await settlements.history(db, ctx.shop_id)
    return {
        "balances": [
            {
                "provider": b.provider,
                "collected": b.collected,
                "settled": b.settled,
                "outstanding": b.outstanding,
            }
            for b in await settlements.balances(db, ctx.shop_id)
        ],
        "items": [
            plain(r, ["id", "provider", "amount", "reference", "note", "staff_id", "created_at"])
            for r in rows
        ],
    }


@router.post("/settlements")
async def record_settlement(
    body: SettlementBody, ctx: Annotated[StaffContext, Depends(require("payments.refund"))], db: DB
) -> dict[str, Any]:
    from paurubot.core import settlements
    from paurubot.payments.engine import PROVIDERS

    if body.provider not in PROVIDERS:
        raise HTTPException(status_code=422, detail="unknown_provider")
    row = await settlements.record(
        db,
        ctx.shop_id,
        provider=body.provider,
        amount=body.amount,
        reference=body.reference,
        note=body.note,
        staff_id=ctx.staff.id,
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="settlement.record",
        entity="settlement",
        entity_id=row.id,
        after=body.model_dump(),
    )
    return {"id": row.id}
