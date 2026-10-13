"""Discount codes (GRW-05) and broadcasts (GRW-06)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

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
    "blocked",
    "deleted",
    "skipped",
    "scheduled_at",
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
    # A private code for one customer, by Telegram ID (GRW-10).
    customer_telegram_id: int | None = Field(default=None, gt=0)


def _validate(body: CodeBody) -> None:
    if body.kind == DiscountKind.PERCENT and body.value > 100:
        raise HTTPException(status_code=422, detail="percent_over_100")


async def _code_values(db: DB, ctx: StaffContext, body: CodeBody) -> dict[str, Any]:
    from paurubot.core import customers

    values = body.model_dump(exclude={"customer_telegram_id"})
    values["customer_id"] = None
    if body.customer_telegram_id:
        owner = await customers.by_telegram(db, ctx.shop_id, body.customer_telegram_id)
        if owner is None:
            raise HTTPException(status_code=422, detail="customer_not_found")
        values["customer_id"] = owner.id
    return values


async def _code_out(db: DB, row: DiscountCode) -> dict[str, Any]:
    from paurubot.db.models import Customer

    out = plain(row, CODE_FIELDS)
    owner = await db.get(Customer, row.customer_id) if row.customer_id else None
    out["customer_telegram_id"] = owner.telegram_id if owner else None
    return out


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
    return {"items": [await _code_out(db, c) for c in rows], "total": total}


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
    row = DiscountCode(shop_id=ctx.shop_id, **await _code_values(db, ctx, body))
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
    if row.customer_id and row.is_active:
        await queue.enqueue(
            db,
            "notify",
            "private_code",
            {"customer_id": row.customer_id, "code": row.code},
            shop_id=ctx.shop_id,
            dedupe_key=f"discount:{row.id}:private",
        )
    return await _code_out(db, row)


@router.put("/discounts/{code_id}")
async def update_code(
    code_id: int, body: CodeBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    _validate(body)
    row = await owned(db, DiscountCode, code_id, ctx)
    before = snapshot(row)
    for key, value in (await _code_values(db, ctx, body)).items():
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
    return await _code_out(db, row)


AudienceKind = Literal["all", "resellers", "buyers", "active", "inactive", "never_bought"]


class BroadcastBody(BaseModel):
    kind: AudienceKind = "all"
    text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, str]] = Field(default_factory=list, max_length=6)
    resellers_only: bool = False
    buyers_only: bool = False
    locale: str | None = Field(default=None, max_length=8)
    # F-16: start later instead of now (timezone-aware, at most 60 days ahead)
    scheduled_at: datetime | None = None


def _check_buttons(body: BroadcastBody) -> None:
    for button in body.buttons:
        url = button.get("url", "")
        if not button.get("text") or not url.startswith(("https://", "tg://")):
            raise HTTPException(status_code=422, detail="bad_button")


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
    from paurubot.core import broadcasts

    _check_buttons(body)
    if not broadcasts.schedule_ok(body.scheduled_at):
        raise HTTPException(status_code=422, detail="schedule_invalid")
    audience = {
        "kind": body.kind,
        "resellers_only": body.resellers_only,
        "buyers_only": body.buyers_only,
        "locale": body.locale,
    }
    row = Broadcast(
        shop_id=ctx.shop_id,
        created_by_staff_id=ctx.staff.id,
        text=body.text,
        buttons=body.buttons,
        audience=audience,
        status=JobStatus.QUEUED,
        scheduled_at=body.scheduled_at,
    )
    db.add(row)
    await db.flush()
    await broadcasts.start(db, row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.queued",
        entity="broadcast",
        entity_id=row.id,
        after={"scheduled_at": body.scheduled_at.isoformat()} if body.scheduled_at else None,
    )
    return plain(row, BROADCAST_FIELDS)


@router.post("/broadcasts/test")
async def send_test_broadcast(
    body: BroadcastBody, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    # Send the message only to the staff member's own Telegram chat before sending it to everyone.
    from paurubot.notifications import handlers, telegram

    _check_buttons(body)
    chat_id = ctx.staff.telegram_id
    if not chat_id:
        raise HTTPException(status_code=422, detail="no_telegram")
    draft = Broadcast(shop_id=ctx.shop_id, text=body.text, buttons=body.buttons, audience={})
    bot = await telegram.bot_for(ctx.shop_id)
    if bot is None:
        raise HTTPException(status_code=502, detail="telegram_unavailable")
    try:
        await telegram.deliver(handlers.broadcast_copier(bot, draft)(int(chat_id)))
    except telegram.Undeliverable as exc:
        raise HTTPException(status_code=422, detail="no_telegram") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="telegram_unavailable") from exc
    return {"sent_to": int(chat_id)}


@router.post("/broadcasts/{broadcast_id}/cancel")
async def cancel_broadcast(
    broadcast_id: int, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    from paurubot.core import broadcasts

    row = await owned(db, Broadcast, broadcast_id, ctx)
    if not await broadcasts.cancel(db, row):
        raise HTTPException(status_code=409, detail="already_finished")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.cancel",
        entity="broadcast",
        entity_id=row.id,
    )
    return plain(row, BROADCAST_FIELDS)


@router.post("/broadcasts/{broadcast_id}/pause")
async def pause_broadcast(
    broadcast_id: int, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    from paurubot.core import broadcasts

    row = await owned(db, Broadcast, broadcast_id, ctx)
    if not await broadcasts.pause(db, row):
        raise HTTPException(status_code=409, detail="not_pausable")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.pause",
        entity="broadcast",
        entity_id=row.id,
    )
    return plain(row, BROADCAST_FIELDS)


@router.post("/broadcasts/{broadcast_id}/resume")
async def resume_broadcast(
    broadcast_id: int, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    from paurubot.core import broadcasts

    row = await owned(db, Broadcast, broadcast_id, ctx)
    if not await broadcasts.resume(db, row):
        raise HTTPException(status_code=409, detail="not_paused")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.resume",
        entity="broadcast",
        entity_id=row.id,
    )
    return plain(row, BROADCAST_FIELDS)


@router.get("/broadcasts/audience")
async def audience_size(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))],
    db: DB,
    kind: AudienceKind = "all",
    locale: str | None = None,
    resellers_only: bool = False,
    buyers_only: bool = False,
) -> dict[str, int]:
    from paurubot.notifications.handlers import _audience

    draft = Broadcast(
        shop_id=ctx.shop_id,
        audience={
            "kind": kind,
            "locale": locale,
            "resellers_only": resellers_only,
            "buyers_only": buyers_only,
        },
    )
    total = await db.scalar(select(func.count(Customer.id)).where(*_audience(draft)))
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


# ---------------------------------------------------------------- lotteries (GRW-04)

Sender = Annotated[StaffContext, Depends(require("broadcast.send"))]
LOTTERY_FIELDS = [
    "id",
    "title",
    "prize",
    "prize_amount",
    "winners",
    "mode",
    "starts_at",
    "ends_at",
    "status",
    "result",
]


class LotteryBody(BaseModel):
    title: str = Field(min_length=1, max_length=96)
    prize: str = Field(min_length=1, max_length=255)
    prize_amount: int = Field(default=0, ge=0, le=10**10)
    winners: int = Field(default=1, ge=1, le=100)
    mode: str = Field(default="join", pattern=r"^(join|purchases)$")
    starts_at: datetime | None = None
    ends_at: datetime


@router.get("/lotteries")
async def list_lotteries(ctx: Sender, db: DB) -> dict[str, Any]:
    from paurubot.core import growth
    from paurubot.db.models import Customer, Lottery

    rows = list(
        await db.scalars(
            select(Lottery).where(Lottery.shop_id == ctx.shop_id).order_by(Lottery.id.desc()).limit(50)
        )
    )
    items = []
    for row in rows:
        data = plain(row, LOTTERY_FIELDS)
        pool = await growth.tickets(db, row)
        data["entrants"], data["tickets"] = len(pool), sum(pool.values())
        names = []
        for customer_id in (row.result or {}).get("winners", []):
            customer = await db.get(Customer, int(customer_id))
            names.append(
                {"id": int(customer_id), "name": customer.display_name if customer else f"#{customer_id}"}
            )
        data["winner_names"] = names
        items.append(data)
    return {"items": items}


@router.post("/lotteries")
async def create_lottery(body: LotteryBody, ctx: Sender, db: DB) -> dict[str, Any]:
    from paurubot.db.base import utcnow
    from paurubot.db.models import Lottery

    starts = body.starts_at or utcnow()
    if body.ends_at <= starts or body.ends_at <= utcnow():
        raise HTTPException(status_code=422, detail="lottery_dates_invalid")
    row = Lottery(shop_id=ctx.shop_id, **{**body.model_dump(), "starts_at": starts})
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="lottery.create",
        entity="lottery",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, LOTTERY_FIELDS)


@router.post("/lotteries/{lottery_id}/{action}")
async def lottery_action(lottery_id: int, action: str, ctx: Sender, db: DB) -> dict[str, Any]:
    """`draw` now (instead of waiting for the end time) or `cancel` an open lottery."""
    from paurubot.core import growth
    from paurubot.db.models import Lottery

    row = await owned(db, Lottery, lottery_id, ctx)
    if row.status != "open":
        raise HTTPException(status_code=409, detail="lottery_closed")
    if action == "cancel":
        row.status = "canceled"
    elif action == "draw":
        await growth.draw(db, row)
        await queue.enqueue(
            db,
            "notify",
            "lottery_result",
            {"lottery_id": row.id},
            shop_id=ctx.shop_id,
            dedupe_key=f"lottery:{row.id}:result",
        )
    else:
        raise HTTPException(status_code=404, detail="not_found")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"lottery.{action}",
        entity="lottery",
        entity_id=row.id,
    )
    return plain(row, LOTTERY_FIELDS)
