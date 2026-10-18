"""Owned inbox adapters and permission-gated shop publishing; no Telegram broadcasting."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from paurubot.api.deps import DB, StaffContext, require
from paurubot.api.webapp_auth import WebApp
from paurubot.core import announcements as core
from paurubot.db.models.announcements import Announcement

router = APIRouter()
Publisher = Annotated[StaffContext, Depends(require("broadcast.send"))]
Before = Annotated[int | None, Query(gt=0, le=2**63 - 1)]
Limit = Annotated[int, Query(ge=1, le=50)]
RowID = Annotated[int, Path(gt=0, le=2**63 - 1)]


class MarkBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["seen", "read"]


def _error(exc: core.InboxError) -> HTTPException:
    return HTTPException(status_code=404 if exc.key == "announcement_not_found" else 409, detail=exc.key)


def _staff(ctx: StaffContext) -> None:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/api/webapp/announcements")
async def customer_inbox(
    ctx: WebApp,
    response: Response,
    before: Before = None,
    limit: Limit = 20,
    unread: bool = False,
) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await core.inbox(
            ctx.session,
            ctx.shop.id,
            ctx.customer.id,
            before=before,
            limit=limit,
            unread=unread,
        )
    except core.InboxError as exc:
        raise _error(exc) from exc


@router.post("/api/webapp/announcements/{row_id}/state")
async def customer_state(row_id: RowID, body: MarkBody, ctx: WebApp, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await core.mark(ctx.session, ctx.shop.id, ctx.customer.id, row_id, body.action)
    except core.InboxError as exc:
        raise _error(exc) from exc
    await ctx.session.commit()
    return {"item": result}


@router.get("/api/admin/announcements")
async def staff_inbox(
    db: DB, ctx: Publisher, response: Response, before: Before = None, limit: Limit = 20
) -> dict[str, Any]:
    _staff(ctx)
    response.headers["Cache-Control"] = "no-store"
    query = select(Announcement).where(Announcement.shop_id == ctx.shop_id)
    if before is not None:
        query = query.where(Announcement.id < before)
    rows = (await db.scalars(query.order_by(Announcement.id.desc()).limit(limit + 1))).all()
    return {
        "items": [core.item(row) for row in rows[:limit]],
        "next_before": rows[limit - 1].id if len(rows) > limit else None,
    }


@router.post("/api/admin/announcements")
async def staff_publish(body: core.Publish, db: DB, ctx: Publisher, response: Response) -> dict[str, Any]:
    _staff(ctx)
    response.headers["Cache-Control"] = "no-store"
    try:
        row = await core.publish(db, ctx.shop_id, ctx.actor, body)
    except core.InboxError as exc:
        raise _error(exc) from exc
    await db.commit()
    return {"item": core.item(row)}


@router.post("/api/admin/announcements/{row_id}/revoke")
async def staff_revoke(row_id: RowID, db: DB, ctx: Publisher, response: Response) -> dict[str, Any]:
    _staff(ctx)
    response.headers["Cache-Control"] = "no-store"
    try:
        row = await core.revoke(db, ctx.shop_id, ctx.actor, row_id)
    except core.InboxError as exc:
        raise _error(exc) from exc
    await db.commit()
    return {"item": core.item(row)}
