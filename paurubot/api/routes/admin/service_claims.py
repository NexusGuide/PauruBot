"""Customers' requests to add a service they already have on the shop's panels: staff approve or
reject them here (settings › services › add a previous service)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import service_claims
from paurubot.db.models import Customer, Panel, ServiceClaim
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reviewer = Annotated[StaffContext, Depends(require("services.write"))]


async def _view(db: DB, row: ServiceClaim) -> dict[str, Any]:
    return service_claims.view(
        row, await db.get(Customer, row.customer_id), await db.get(Panel, row.panel_id)
    )


@router.get("/service-claims")
async def list_claims(
    ctx: Reviewer, db: DB, status: Literal["pending", "approved", "rejected", "all"] = "all"
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    stmt = select(ServiceClaim).where(ServiceClaim.shop_id == ctx.shop_id)
    if status != "all":
        stmt = stmt.where(ServiceClaim.status == status)
    rows = list(await db.scalars(stmt.order_by(ServiceClaim.id.desc()).limit(200)))
    return {"items": [await _view(db, row) for row in rows]}


class Decision(BaseModel):
    approve: bool


@router.post("/service-claims/{claim_id}")
async def decide(claim_id: int, body: Decision, ctx: Reviewer, db: DB) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    row = await db.scalar(
        select(ServiceClaim)
        .where(ServiceClaim.id == claim_id, ServiceClaim.shop_id == ctx.shop_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        service = await service_claims.decide(db, row, body.approve, f"staff:{ctx.actor.id}")
    except service_claims.ClaimError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=exc.key) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service_claim.approved" if body.approve else "service_claim.rejected",
        entity="customer",
        entity_id=row.customer_id,
        after={
            "panel_id": row.panel_id,
            "username": row.panel_username,
            "service_id": service.id if service else None,
        },
    )
    await queue.enqueue(db, "notify", "service_claim_decided", {"claim_id": row.id}, shop_id=ctx.shop_id)
    await db.commit()
    return {"claim": await _view(db, row)}
