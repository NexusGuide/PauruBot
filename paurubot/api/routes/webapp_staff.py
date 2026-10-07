"""Staff and reseller tools inside the Mini App (APP-03).

The Mini App already proves who the Telegram user is. When that user is also an active staff
account linked to the same Telegram id, they get a short management view: the dashboard figures
their role may see and, for reviewers, the payments waiting for a decision. Every action goes
through the same handlers as the admin panel, with the same permission checks and audit trail.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot.api.deps import StaffContext
from paurubot.api.webapp_auth import WebApp, WebAppContext
from paurubot.auth import rbac
from paurubot.auth import staff as staff_service
from paurubot.db.models import Payment, Staff
from paurubot.payments import engine as payments

router = APIRouter(prefix="/api/webapp/staff", tags=["webapp"])
REVIEW_PAGE = 20


async def staff_for(ctx: WebAppContext) -> StaffContext | None:
    staff = await ctx.session.scalar(
        select(Staff).where(
            Staff.shop_id == ctx.shop.id,
            Staff.telegram_id == ctx.customer.telegram_id,
            Staff.is_active.is_(True),
        )
    )
    if staff is None:
        return None
    allow = await staff_service.reseller_allow_list(ctx.session, staff)
    return StaffContext(
        staff=staff,
        shop_id=ctx.shop.id,
        session=None,  # type: ignore[arg-type]  # no browser session: the Telegram sign-in stands in
        session_id="webapp",
        ip="telegram-webapp",
        permissions=frozenset(rbac.permissions_for(staff.role, allow)),
    )


async def _need(ctx: WebAppContext, permission: str, *, shop_wide: bool = False) -> StaffContext:
    staff = await staff_for(ctx)
    if staff is None or not staff.can(permission) or (shop_wide and staff.scoped):
        raise HTTPException(status_code=403, detail="forbidden")
    return staff


async def describe(ctx: WebAppContext) -> dict[str, Any] | None:
    """What the Mini App should offer this user, or None for ordinary customers."""
    staff = await staff_for(ctx)
    if staff is None:
        return None
    return {
        "role": staff.staff.role.value,
        "scoped": staff.scoped,
        "dashboard": staff.can("dashboard.view"),
        "review": staff.can("payments.review") and not staff.scoped,
    }


@router.get("/dashboard")
async def dashboard(ctx: WebApp) -> dict[str, Any]:
    from paurubot.api.routes.admin.dashboard import dashboard as admin_dashboard

    staff = await _need(ctx, "dashboard.view")
    data = await admin_dashboard(staff, ctx.session)
    data.pop("series", None)  # the Mini App shows figures, not charts
    return data


def _review_item(payment: Payment) -> dict[str, Any]:
    return {
        "id": payment.id,
        "provider": payment.provider,
        "amount": payment.amount,
        "review_amount": payments.disputed_credit(payment),
        "customer": payment.customer.display_name if payment.customer else None,
        "customer_id": payment.customer_id,
        "created_at": payment.created_at.isoformat() if payment.created_at else None,
        "has_receipt": bool(payment.receipt_file_id),
        "receipt_text": payment.receipt_text,
        "duplicate_of_id": payment.duplicate_of_id,
    }


@router.get("/reviews")
async def reviews(ctx: WebApp) -> dict[str, Any]:
    await _need(ctx, "payments.review", shop_wide=True)
    rows = await ctx.session.scalars(
        select(Payment)
        .where(Payment.shop_id == ctx.shop.id, payments.review_filter())
        .order_by(Payment.id)
        .limit(REVIEW_PAGE * 3)
    )
    items = [_review_item(p) for p in rows if payments.awaiting_review(p)][:REVIEW_PAGE]
    return {"items": items}


@router.get("/reviews/{payment_id}/receipt")
async def receipt(payment_id: int, ctx: WebApp) -> Response:
    from paurubot.api.routes.admin.commerce import receipt_image

    staff = await _need(ctx, "payments.review", shop_wide=True)
    return await receipt_image(payment_id, staff, ctx.session)


@router.post("/reviews/{payment_id}/approve")
async def approve(payment_id: int, ctx: WebApp) -> dict[str, Any]:
    from paurubot.api.routes.admin.commerce import approve as admin_approve

    staff = await _need(ctx, "payments.review", shop_wide=True)
    result = await admin_approve(payment_id, staff, ctx.session)
    return {"status": result["payment"]["status"], "order_settled": result["order_settled"]}


class RejectBody(BaseModel):
    reason: str = Field(default="", max_length=200)


@router.post("/reviews/{payment_id}/reject")
async def reject(payment_id: int, body: RejectBody, ctx: WebApp) -> dict[str, Any]:
    from paurubot.api.routes.admin.commerce import RejectBody as AdminReject
    from paurubot.api.routes.admin.commerce import reject as admin_reject

    staff = await _need(ctx, "payments.review", shop_wide=True)
    result = await admin_reject(payment_id, AdminReject(reason=body.reason), staff, ctx.session)
    return {"status": result["payment"]["status"]}
