"""Customers' service cancellation requests when staff approval is enabled (SVC-14)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import cancellation
from paurubot.db.models import CancellationRequest, Customer, Service
from paurubot.panels import PanelError
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reviewer = Annotated[StaffContext, Depends(require("services.write"))]


async def _view(db: DB, row: CancellationRequest) -> dict[str, Any]:
    return cancellation.view(
        row, await db.get(Customer, row.customer_id), await db.get(Service, row.service_id)
    )


@router.get("/cancellations")
async def list_cancellations(
    ctx: Reviewer, db: DB, status: Literal["pending", "approved", "rejected", "all"] = "all"
) -> dict[str, Any]:
    """List customers' service cancellation requests for the current shop."""
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    stmt = select(CancellationRequest).where(CancellationRequest.shop_id == ctx.shop_id)
    if status != "all":
        stmt = stmt.where(CancellationRequest.status == status)
    rows = list(await db.scalars(stmt.order_by(CancellationRequest.id.desc()).limit(200)))
    return {"items": [await _view(db, row) for row in rows]}


class Decision(BaseModel):
    approve: bool
    refund_amount: int | None = Field(default=None, ge=0, le=10**10)
    note: str = Field(default="", max_length=255)


@router.post("/cancellations/{request_id}")
async def decide_cancellation(request_id: int, body: Decision, ctx: Reviewer, db: DB) -> dict[str, Any]:
    """Approve or reject a pending service cancellation request."""
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    row = await db.scalar(
        select(CancellationRequest)
        .where(CancellationRequest.id == request_id, CancellationRequest.shop_id == ctx.shop_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        refund = await cancellation.decide(
            db,
            row,
            approve=body.approve,
            actor=f"staff:{ctx.actor.id}",
            refund_override=body.refund_amount,
            note=body.note,
        )
    except cancellation.CancelError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=exc.key) from None
    except PanelError:
        await db.rollback()
        raise HTTPException(status_code=502, detail="panel_busy") from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="cancellation.approved" if body.approve else "cancellation.rejected",
        entity="service",
        entity_id=row.service_id,
        after={"request_id": row.id, "refund": refund, "note": row.staff_note},
    )
    await queue.enqueue(
        db,
        "notify",
        "cancellation_decided",
        {"request_id": row.id},
        shop_id=ctx.shop_id,
        dedupe_key=f"cancel_decided:{row.id}",
    )
    await db.commit()
    return {"request": await _view(db, row)}
