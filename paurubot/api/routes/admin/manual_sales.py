"""Manual inventory console. Payload reads and mutations are audited, never logged."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select

from paurubot.api.admin_common import owned
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import manual_stock, stock
from paurubot.db.base import utcnow
from paurubot.db.models import Panel, Plan, StockItem

router = APIRouter(prefix="/api/admin/manual-sales", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("catalog.read"))]
Writer = Annotated[StaffContext, Depends(require("catalog.write"))]


def shop_scope(ctx: StaffContext) -> None:
    if ctx.scoped:
        raise HTTPException(403, "forbidden")


@router.get("")
async def inventory(
    ctx: Reader,
    db: DB,
    plan_id: int | None = None,
    status: Literal["available", "sold", "disabled", "reserved"] | None = None,
    search: str = Query("", max_length=96),
    remaining_days: int | None = Query(None, ge=0, le=3650),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    shop_scope(ctx)
    plans = list(
        await db.scalars(select(Plan).where(Plan.shop_id == ctx.shop_id).order_by(Plan.sort, Plan.id))
    )
    panels = list(await db.scalars(select(Panel).where(Panel.shop_id == ctx.shop_id).order_by(Panel.id)))
    totals = {stock.AVAILABLE: 0, stock.SOLD: 0, stock.DISABLED: 0}
    for state, n in (
        await db.execute(
            select(StockItem.status, func.count())
            .where(StockItem.shop_id == ctx.shop_id)
            .group_by(StockItem.status)
        )
    ).all():
        totals[state] = n
    stmt = (
        select(StockItem, Plan)
        .join(Plan, Plan.id == StockItem.plan_id)
        .where(StockItem.shop_id == ctx.shop_id)
    )
    if plan_id:
        stmt = stmt.where(StockItem.plan_id == plan_id)
    if status:
        stmt = stmt.where(StockItem.status == status)
    if search.strip():
        stmt = stmt.where(
            func.coalesce(StockItem.name, StockItem.note, "").icontains(search.strip(), autoescape=True)
        )
    if remaining_days is not None:
        stmt = stmt.where(
            StockItem.status == stock.SOLD,
            Plan.days > 0,
            (Plan.days * 86400 - func.extract("epoch", func.now() - StockItem.sold_at))
            <= remaining_days * 86400,
        )
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(StockItem.id.desc()).offset(offset).limit(limit))).all()
    items = []
    for item, plan in rows:
        expires = item.sold_at + timedelta(days=plan.days) if item.sold_at and plan.days else None
        items.append(
            {
                "id": item.id,
                "name": item.name or item.note or f"#{item.id}",
                "plan_id": item.plan_id,
                "plan": plan.title,
                "status": item.status,
                "parts": item.part_count,
                "bytes": item.content_bytes,
                "customer_id": item.customer_id,
                "order_id": item.order_id,
                "created_at": item.created_at.isoformat(),
                "sold_at": item.sold_at.isoformat() if item.sold_at else None,
                "expires_at": expires.isoformat() if expires else None,
                "remaining_days": max(0, (expires - utcnow()).days) if expires else None,
            }
        )
    return {
        "items": items,
        "total": total,
        "counts": totals,
        "storage": {
            "used": await manual_stock.storage_used(db, ctx.shop_id),
            "limit": manual_stock.STORAGE_LIMIT,
        },
        "plans": [
            {
                "id": p.id,
                "title": p.title,
                "delivery": p.delivery,
                "panel_id": p.panel_id,
                "stock_plan_id": p.national_stock_plan_id,
                "active": p.is_active,
            }
            for p in plans
        ],
        "panels": [{"id": p.id, "name": p.name, "national_mode": p.national_mode} for p in panels],
    }


MAX_IMPORT_REQUEST = 12 * 1024 * 1024


async def bounded_import(request: Request) -> manual_stock.Import:
    # Bound the request BEFORE JSON/base64 parsing, including chunked requests.
    length = request.headers.get("content-length")
    if length and (not length.isdecimal() or len(length) > 10 or int(length) > MAX_IMPORT_REQUEST):
        raise HTTPException(413, "stock_import_too_large")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_IMPORT_REQUEST:
            raise HTTPException(413, "stock_import_too_large")
        content.extend(chunk)
    try:
        return manual_stock.Import.model_validate_json(content)
    except ValidationError:
        raise HTTPException(422, "validation_error") from None


@router.post("/import")
async def import_inventory(
    body: Annotated[manual_stock.Import, Depends(bounded_import)], ctx: Writer, db: DB
) -> dict[str, int]:
    shop_scope(ctx)
    plan = await owned(db, Plan, body.plan_id, ctx)
    try:
        result = await manual_stock.add(db, plan, body)
    except stock.StockError as exc:
        raise HTTPException(422, str(exc)) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="stock.import",
        entity="plan",
        entity_id=plan.id,
        after={**result, "mode": body.mode},
    )
    return result


class Bulk(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["delete", "disable", "enable"]


@router.post("/bulk")
async def bulk_inventory(body: Bulk, ctx: Writer, db: DB) -> dict[str, int]:
    shop_scope(ctx)
    ids = sorted(set(body.ids))
    rows = list(
        await db.scalars(
            select(StockItem)
            .where(StockItem.shop_id == ctx.shop_id, StockItem.id.in_(ids))
            .order_by(StockItem.id)
            .with_for_update()
        )
    )
    if len(rows) != len(ids):
        raise HTTPException(404, "not_found")
    if any(i.status == stock.SOLD or i.order_id is not None for i in rows):
        raise HTTPException(409, "stock_sold")
    for item in rows:
        if body.action == "delete":
            await db.delete(item)
        else:
            item.status = stock.AVAILABLE if body.action == "enable" else stock.DISABLED
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"stock.{body.action}",
        entity="stock",
        after={"ids": ids},
    )
    return {"changed": len(rows)}


@router.get("/{item_id}/content")
async def content(item_id: int, ctx: Writer, db: DB, response: Response) -> dict[str, Any]:
    shop_scope(ctx)
    item = await owned(db, StockItem, item_id, ctx)
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="stock.view", entity="stock", entity_id=item.id
    )
    response.headers["Cache-Control"] = "no-store"
    return {"id": item.id, "parts": [p.model_dump() for p in manual_stock.parts(item)]}


class Mapping(BaseModel):
    stock_plan_id: int | None = Field(default=None, gt=0)


@router.put("/mapping/{plan_id}")
async def mapping(plan_id: int, body: Mapping, ctx: Writer, db: DB) -> dict[str, bool]:
    shop_scope(ctx)
    if not ctx.can("panels.write"):
        raise HTTPException(403, "forbidden")
    plan = await owned(db, Plan, plan_id, ctx)
    if plan.delivery == "stock" or not plan.panel_id:
        raise HTTPException(422, "national_requires_fixed_panel")
    if body.stock_plan_id:
        target = await owned(db, Plan, body.stock_plan_id, ctx)
        if target.delivery != "stock" or not target.is_active:
            raise HTTPException(422, "plan_unavailable")
    plan.national_stock_plan_id = body.stock_plan_id
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="national.mapping",
        entity="plan",
        entity_id=plan.id,
        after={"stock_plan_id": body.stock_plan_id},
    )
    return {"ok": True}


class Switch(BaseModel):
    enabled: bool


@router.put("/national/{panel_id}")
async def national_switch(panel_id: int, body: Switch, ctx: Writer, db: DB) -> dict[str, bool]:
    shop_scope(ctx)
    if not ctx.can("panels.write"):
        raise HTTPException(403, "forbidden")
    panel = await owned(db, Panel, panel_id, ctx)
    panel.national_mode = body.enabled
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="national.switch",
        entity="panel",
        entity_id=panel.id,
        after={"enabled": body.enabled},
    )
    return {"enabled": panel.national_mode}
