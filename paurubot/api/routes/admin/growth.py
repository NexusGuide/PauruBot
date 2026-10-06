"""Discount codes (GRW-05) and broadcasts (GRW-06)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.admin_common import Page, owned, page_params, paginate, plain
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.db.models import (
    Broadcast,
    Customer,
    DiscountCode,
    DiscountKind,
    GiftCode,
    GiftRedemption,
    JobStatus,
)
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])
CODE_FIELDS = [
    "id",
    "code",
    "kind",
    "value",
    "max_discount",
    "min_amount",
    "max_uses",
    "used_count",
    "per_customer_limit",
    "plan_ids",
    "first_purchase_only",
    "sticky",
    "starts_at",
    "expires_at",
    "is_active",
    "note",
    "created_at",
]
BROADCAST_FIELDS = [
    "id",
    "text",
    "audience",
    "status",
    "total",
    "sent",
    "failed",
    "created_at",
    "finished_at",
]


class CodeBody(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    kind: DiscountKind = DiscountKind.PERCENT
    value: int = Field(ge=1, le=10**10)
    max_discount: int | None = Field(default=None, ge=1)
    min_amount: int = Field(default=0, ge=0)
    max_uses: int | None = Field(default=None, ge=1)
    per_customer_limit: int = Field(default=1, ge=0)
    plan_ids: list[int] = Field(default_factory=list)
    first_purchase_only: bool = False
    sticky: bool = False
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True
    note: str | None = Field(default=None, max_length=255)


def _validate(body: CodeBody) -> None:
    if body.kind == DiscountKind.PERCENT and body.value > 100:
        raise HTTPException(status_code=422, detail="percent_over_100")


@router.get("/discounts")
async def list_codes(
    ctx: Annotated[StaffContext, Depends(require("discounts.write"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
) -> dict[str, Any]:
    rows, total = await paginate(
        db,
        select(DiscountCode).where(DiscountCode.shop_id == ctx.shop_id).order_by(DiscountCode.id.desc()),
        page,
    )
    return {"items": [plain(c, CODE_FIELDS) for c in rows], "total": total}


@router.post("/discounts")
async def create_code(
    body: CodeBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    _validate(body)
    exists = await db.scalar(
        select(DiscountCode.id).where(
            DiscountCode.shop_id == ctx.shop_id, func.lower(DiscountCode.code) == body.code.lower()
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="code_exists")
    row = DiscountCode(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="discount.create",
        entity="discount",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, CODE_FIELDS)


@router.put("/discounts/{code_id}")
async def update_code(
    code_id: int, body: CodeBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    _validate(body)
    row = await owned(db, DiscountCode, code_id, ctx)
    before = snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="discount.update",
        entity="discount",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, CODE_FIELDS)


class BroadcastBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, str]] = Field(default_factory=list, max_length=6)
    resellers_only: bool = False
    buyers_only: bool = False
    locale: str | None = Field(default=None, max_length=8)


@router.get("/broadcasts")
async def list_broadcasts(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
) -> dict[str, Any]:
    rows, total = await paginate(
        db, select(Broadcast).where(Broadcast.shop_id == ctx.shop_id).order_by(Broadcast.id.desc()), page
    )
    return {"items": [plain(b, BROADCAST_FIELDS) for b in rows], "total": total}


@router.post("/broadcasts")
async def create_broadcast(
    body: BroadcastBody, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    for button in body.buttons:
        url = button.get("url", "")
        if not button.get("text") or not url.startswith(("https://", "tg://")):
            raise HTTPException(status_code=422, detail="bad_button")
    audience = {"resellers_only": body.resellers_only, "buyers_only": body.buyers_only, "locale": body.locale}
    row = Broadcast(
        shop_id=ctx.shop_id,
        created_by_staff_id=ctx.staff.id,
        text=body.text,
        buttons=body.buttons,
        audience=audience,
        status=JobStatus.QUEUED,
    )
    db.add(row)
    await db.flush()
    await queue.enqueue(
        db,
        "broadcast",
        "run",
        {"broadcast_id": row.id},
        shop_id=ctx.shop_id,
        dedupe_key=f"broadcast:{row.id}",
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.queued",
        entity="broadcast",
        entity_id=row.id,
    )
    return plain(row, BROADCAST_FIELDS)


@router.post("/broadcasts/{broadcast_id}/cancel")
async def cancel_broadcast(
    broadcast_id: int, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    row = await owned(db, Broadcast, broadcast_id, ctx)
    if row.status in {JobStatus.DONE, JobStatus.CANCELED}:
        raise HTTPException(status_code=409, detail="already_finished")
    row.status = JobStatus.CANCELED
    return plain(row, BROADCAST_FIELDS)


@router.get("/broadcasts/audience")
async def audience_size(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, int]:
    total = await db.scalar(
        select(func.count(Customer.id)).where(
            Customer.shop_id == ctx.shop_id, Customer.is_banned.is_(False), Customer.blocked_bot_at.is_(None)
        )
    )
    return {"reachable": int(total or 0)}


# --- gift codes (FIN-03) -----------------------------------------------------------

GIFT_FIELDS = [
    "id",
    "code",
    "amount",
    "max_uses",
    "used_count",
    "new_customers_only",
    "expires_at",
    "is_active",
    "note",
    "created_at",
]


class GiftBody(BaseModel):
    code: str = Field(min_length=4, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    amount: int = Field(ge=1000, le=10**10)
    max_uses: int | None = Field(default=None, ge=1)
    new_customers_only: bool = False
    expires_at: datetime | None = None
    is_active: bool = True
    note: str | None = Field(default=None, max_length=255)


@router.get("/gift-codes")
async def list_gifts(
    ctx: Annotated[StaffContext, Depends(require("discounts.write"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
) -> dict[str, Any]:
    rows, total = await paginate(
        db, select(GiftCode).where(GiftCode.shop_id == ctx.shop_id).order_by(GiftCode.id.desc()), page
    )
    return {"items": [plain(c, GIFT_FIELDS) for c in rows], "total": total}


@router.post("/gift-codes")
async def create_gift(
    body: GiftBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    if not ctx.can("wallet.adjust"):
        raise HTTPException(status_code=403, detail="forbidden")  # a gift code is money
    exists = await db.scalar(
        select(GiftCode.id).where(
            GiftCode.shop_id == ctx.shop_id, func.lower(GiftCode.code) == body.code.lower()
        )
    )
    if exists:
        raise HTTPException(status_code=409, detail="code_exists")
    row = GiftCode(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="gift.create",
        entity="gift",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, GIFT_FIELDS)


@router.put("/gift-codes/{code_id}")
async def update_gift(
    code_id: int, body: GiftBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    if not ctx.can("wallet.adjust"):
        raise HTTPException(status_code=403, detail="forbidden")
    row = await owned(db, GiftCode, code_id, ctx)
    before = snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="gift.update",
        entity="gift",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, GIFT_FIELDS)


@router.get("/gift-codes/{code_id}/redemptions")
async def gift_redemptions(
    code_id: int, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    await owned(db, GiftCode, code_id, ctx)
    rows = await db.execute(
        select(GiftRedemption, Customer)
        .join(Customer, Customer.id == GiftRedemption.customer_id)
        .where(GiftRedemption.code_id == code_id)
        .order_by(GiftRedemption.id.desc())
        .limit(200)
    )
    return {
        "items": [
            {
                "id": r.id,
                "customer_id": c.id,
                "customer": c.display_name,
                "amount": r.amount,
                "at": r.created_at.isoformat(),
            }
            for r, c in rows.all()
        ]
    }
