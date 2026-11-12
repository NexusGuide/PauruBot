"""Dashboard (ADM-01) and the staff inbox (ADM-02)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert

from paurubot.api.deps import DB, StaffContext, require
from paurubot.db.models import (
    ApplicationStatus,
    Customer,
    InboxDismissal,
    Job,
    JobStatus,
    Order,
    OrderStatus,
    Panel,
    Payment,
    PaymentStatus,
    ResellerApplication,
    Service,
    ServiceStatus,
    Ticket,
    TicketStatus,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
Viewer = Annotated[StaffContext, Depends(require("dashboard.view"))]


@router.get("/dashboard")
async def dashboard(ctx: Viewer, db: DB) -> dict[str, Any]:
    shop = ctx.shop_id
    now = datetime.now(UTC)
    day, week, month = now - timedelta(days=1), now - timedelta(days=7), now - timedelta(days=30)

    async def count(stmt: Any) -> int:
        return int(await db.scalar(stmt) or 0)

    completed = (Order.shop_id == shop, Order.status == OrderStatus.COMPLETED)
    revenue = select(func.coalesce(func.sum(Order.amount), 0)).where(*completed)
    series_rows = await db.execute(
        select(cast(Order.completed_at, Date).label("day"), func.sum(Order.amount), func.count(Order.id))
        .where(*completed, Order.completed_at >= month)
        .group_by("day")
        .order_by("day")
    )
    series = {row[0].isoformat(): {"revenue": int(row[1] or 0), "orders": int(row[2])} for row in series_rows}
    days = [(month + timedelta(days=i + 1)).date().isoformat() for i in range(30)]
    panels = await db.scalars(select(Panel).where(Panel.shop_id == shop).order_by(Panel.id))
    data: dict[str, Any] = {
        "customers": await count(
            select(func.count(Customer.id)).where(Customer.shop_id == shop, Customer.telegram_id > 0)
        ),
        "customers_today": await count(
            select(func.count(Customer.id)).where(
                Customer.shop_id == shop, Customer.telegram_id > 0, Customer.created_at >= day
            )
        ),
        "active_services": await count(
            select(func.count(Service.id)).where(
                Service.shop_id == shop, Service.status == ServiceStatus.ACTIVE
            )
        ),
        "revenue_today": await count(revenue.where(Order.completed_at >= day)),
        "revenue_week": await count(revenue.where(Order.completed_at >= week)),
        "revenue_month": await count(revenue.where(Order.completed_at >= month)),
        "orders_month": await count(
            select(func.count(Order.id)).where(*completed, Order.completed_at >= month)
        ),
        "pending_receipts": await count(
            select(func.count(Payment.id)).where(
                Payment.shop_id == shop, Payment.status == PaymentStatus.VERIFYING
            )
        ),
        "open_tickets": await count(
            select(func.count(Ticket.id)).where(Ticket.shop_id == shop, Ticket.status == TicketStatus.OPEN)
        ),
        "series": [{"day": d, **series.get(d, {"revenue": 0, "orders": 0})} for d in days],
        "panels": [
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind.value,
                "active": p.is_active,
                "healthy": p.is_healthy,
                "checked_at": p.health_checked_at.isoformat() if p.health_checked_at else None,
            }
            for p in panels
        ],
    }
    if ctx.scoped:
        # Resellers see their own numbers only; shop-wide figures stay private.
        from paurubot.api.admin_common import reseller_profile, reseller_scope
        from paurubot.ledger.accounts import wallet_balance

        scope = await reseller_scope(db, ctx) or []
        profile = await reseller_profile(db, ctx)
        data = {
            "active_services": await count(
                select(func.count(Service.id)).where(
                    Service.customer_id.in_(scope), Service.status == ServiceStatus.ACTIVE
                )
            ),
            "orders_month": await count(
                select(func.count(Order.id)).where(
                    Order.customer_id.in_(scope),
                    Order.status == OrderStatus.COMPLETED,
                    Order.completed_at >= month,
                )
            ),
            "spent_month": await count(
                select(func.coalesce(func.sum(Order.amount), 0)).where(
                    Order.customer_id.in_(scope),
                    Order.status == OrderStatus.COMPLETED,
                    Order.completed_at >= month,
                )
            ),
        }
        if profile is not None:
            data["reseller"] = {
                "balance": await wallet_balance(db, profile.shop_id, profile.customer_id),
                "billing_mode": profile.billing_mode,
                "debt": profile.billing_debt,
                "credit_limit": profile.credit_limit,
                "suspended": profile.suspended_at is not None,
                "discount_percent": profile.discount_percent,
                "expires_at": profile.expires_at.isoformat() if profile.expires_at else None,
                "level": 1 if profile.parent_id is None else 2,
                "sub_resellers": len(scope) - 1,
            }
    return data


InboxType = Literal["payment", "ticket", "order", "job", "application"]


@router.get("/inbox")
async def inbox(ctx: Viewer, db: DB) -> dict[str, Any]:
    if ctx.scoped:
        return {"items": []}
    shop = ctx.shop_id
    dismissed = {
        (row.item_type, row.item_id)
        for row in await db.scalars(select(InboxDismissal).where(InboxDismissal.staff_id == ctx.staff.id))
    }
    items: list[dict[str, Any]] = []
    if ctx.can("payments.review"):
        for p in await db.scalars(
            select(Payment)
            .where(Payment.shop_id == shop, Payment.status == PaymentStatus.VERIFYING)
            .order_by(Payment.id)
            .limit(50)
        ):
            items.append(
                {
                    "type": "payment",
                    "id": p.id,
                    "amount": p.amount,
                    "customer": p.customer.staff_label,
                    "duplicate_of": p.duplicate_of_id,
                    "at": p.updated_at.isoformat(),
                }
            )
    if ctx.can("tickets.reply"):
        for ticket in await db.scalars(
            select(Ticket)
            .where(Ticket.shop_id == shop, Ticket.status == TicketStatus.OPEN)
            .order_by(Ticket.updated_at)
            .limit(50)
        ):
            items.append(
                {
                    "type": "ticket",
                    "id": ticket.id,
                    "subject": ticket.subject,
                    "at": ticket.updated_at.isoformat(),
                }
            )
    if ctx.can("orders.read"):
        since = datetime.now(UTC) - timedelta(days=7)
        for order in await db.scalars(
            select(Order)
            .where(
                Order.shop_id == shop,
                Order.status.in_([OrderStatus.FAILED, OrderStatus.REFUNDED]),
                Order.updated_at >= since,
            )
            .order_by(Order.id.desc())
            .limit(50)
        ):
            items.append(
                {
                    "type": "order",
                    "id": order.id,
                    "amount": order.amount,
                    "error": order.error,
                    "at": order.updated_at.isoformat(),
                }
            )
    if ctx.can("settings.read"):
        for job in await db.scalars(
            select(Job)
            .where(Job.shop_id == shop, Job.status == JobStatus.DEAD)
            .order_by(Job.id.desc())
            .limit(50)
        ):
            items.append(
                {
                    "type": "job",
                    "id": job.id,
                    "kind": f"{job.queue}/{job.kind}",
                    "error": job.last_error,
                    "at": job.updated_at.isoformat(),
                }
            )
    if ctx.can("resellers.manage"):
        for app in await db.scalars(
            select(ResellerApplication).where(
                ResellerApplication.shop_id == shop, ResellerApplication.status == ApplicationStatus.PENDING
            )
        ):
            items.append({"type": "application", "id": app.id, "at": app.created_at.isoformat()})
    items = [i for i in items if (i["type"], i["id"]) not in dismissed]
    items.sort(key=lambda i: i["at"])
    return {"items": items}


class Dismiss(BaseModel):
    type: InboxType
    id: int


@router.post("/inbox/reset")
async def reset_dismissed(ctx: Viewer, db: DB) -> dict[str, int]:
    """Bring back every inbox item this staff member dismissed."""
    from sqlalchemy import delete

    result = await db.execute(delete(InboxDismissal).where(InboxDismissal.staff_id == ctx.staff.id))
    return {"restored": int(getattr(result, "rowcount", 0) or 0)}


@router.post("/inbox/dismiss")
async def dismiss(body: Dismiss, ctx: Viewer, db: DB) -> dict[str, bool]:
    await db.execute(
        insert(InboxDismissal)
        .values(staff_id=ctx.staff.id, item_type=body.type, item_id=body.id, created_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["staff_id", "item_type", "item_id"])
    )
    return {"ok": True}
