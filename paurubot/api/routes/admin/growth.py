"""Discount codes (GRW-05) and broadcasts (GRW-06)."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import func, select

from paurubot.api.admin_common import Page, owned, page_params, paginate, plain
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.db.models import (
    AccountPlan,
    Broadcast,
    Customer,
    DiscountCode,
    DiscountKind,
    DiscountRedemption,
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
    "account_plan_ids",
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
    account_plan_ids: list[int] = Field(default_factory=list)
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


async def _check_plan_targets(
    db: DB, shop_id: int, plan_ids: list[int], account_plan_ids: list[int]
) -> tuple[list[int], list[int]]:
    from paurubot.db.models import Plan

    clean_plans = sorted({int(i) for i in plan_ids if int(i) > 0})
    if clean_plans:
        found = set(
            await db.scalars(select(Plan.id).where(Plan.shop_id == shop_id, Plan.id.in_(clean_plans)))
        )
        if len(found) != len(clean_plans):
            raise HTTPException(status_code=422, detail="plan_not_found")
    clean_acc = sorted({int(i) for i in account_plan_ids if int(i) > 0})
    if clean_acc:
        found_acc = set(
            await db.scalars(
                select(AccountPlan.id).where(AccountPlan.shop_id == shop_id, AccountPlan.id.in_(clean_acc))
            )
        )
        if len(found_acc) != len(clean_acc):
            raise HTTPException(status_code=422, detail="account_plan_not_found")
    return clean_plans, clean_acc


async def _code_values(db: DB, ctx: StaffContext, body: CodeBody) -> dict[str, Any]:
    from paurubot.core import customers

    values = body.model_dump(exclude={"customer_telegram_id"})
    values["plan_ids"], values["account_plan_ids"] = await _check_plan_targets(
        db, ctx.shop_id, body.plan_ids, body.account_plan_ids
    )
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
    dup = await db.scalar(
        select(DiscountCode.id).where(
            DiscountCode.shop_id == ctx.shop_id,
            DiscountCode.id != code_id,
            func.lower(DiscountCode.code) == body.code.lower(),
        )
    )
    if dup:
        raise HTTPException(status_code=409, detail="code_exists")
    before = snapshot(row)
    prev_customer_id = row.customer_id
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
    if row.customer_id and row.is_active and row.customer_id != prev_customer_id:
        await queue.enqueue(
            db,
            "notify",
            "private_code",
            {"customer_id": row.customer_id, "code": row.code},
            shop_id=ctx.shop_id,
            dedupe_key=f"discount:{row.id}:private:{row.customer_id}",
        )
    return await _code_out(db, row)


@router.get("/discounts/{code_id}/redemptions")
async def discount_redemptions(
    code_id: int, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    await owned(db, DiscountCode, code_id, ctx)
    rows = await db.execute(
        select(DiscountRedemption, Customer)
        .join(Customer, Customer.id == DiscountRedemption.customer_id)
        .where(DiscountRedemption.code_id == code_id)
        .order_by(DiscountRedemption.id.desc())
        .limit(200)
    )
    return {
        "items": [
            {
                "id": r.id,
                "customer_id": c.id,
                "customer": c.staff_label,
                "order_id": r.order_id,
                "amount": r.amount,
                "at": r.created_at.isoformat(),
            }
            for r, c in rows.all()
        ]
    }


@router.delete("/discounts/{code_id}")
async def delete_code(
    code_id: int, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, bool]:
    row = await owned(db, DiscountCode, code_id, ctx)
    before = snapshot(row)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="discount.delete",
        entity="discount",
        entity_id=code_id,
        before=before,
    )
    return {"ok": True}


BULK_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _random_code(prefix: str, length: int = 8) -> str:
    body = "".join(secrets.choice(BULK_ALPHABET) for _ in range(length))
    return f"{prefix}{body}"[:32]


class BulkCodeBody(BaseModel):
    count: int = Field(ge=2, le=100)
    prefix: str = Field(default="", max_length=12, pattern=r"^[A-Za-z0-9_-]*$")
    kind: DiscountKind = DiscountKind.PERCENT
    value: int = Field(ge=1, le=10**10)
    max_discount: int | None = Field(default=None, ge=1)
    min_amount: int = Field(default=0, ge=0)
    max_uses: int | None = Field(default=1, ge=1)
    per_customer_limit: int = Field(default=1, ge=0)
    plan_ids: list[int] = Field(default_factory=list)
    account_plan_ids: list[int] = Field(default_factory=list)
    first_purchase_only: bool = False
    sticky: bool = False
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    is_active: bool = True
    note: str | None = Field(default=None, max_length=255)


@router.post("/discounts/bulk")
async def create_codes_bulk(
    body: BulkCodeBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    if body.kind == DiscountKind.PERCENT and body.value > 100:
        raise HTTPException(status_code=422, detail="percent_over_100")
    clean_plans, clean_acc = await _check_plan_targets(db, ctx.shop_id, body.plan_ids, body.account_plan_ids)
    existing = {
        c.lower()
        for c in await db.scalars(select(DiscountCode.code).where(DiscountCode.shop_id == ctx.shop_id))
    }
    prefix = body.prefix.upper()
    codes: list[str] = []
    while len(codes) < body.count:
        cand = _random_code(prefix)
        if cand.lower() not in existing:
            existing.add(cand.lower())
            codes.append(cand)
    base = body.model_dump(exclude={"count", "prefix"})
    base["plan_ids"] = clean_plans
    base["account_plan_ids"] = clean_acc
    rows: list[DiscountCode] = []
    for code in codes:
        row = DiscountCode(shop_id=ctx.shop_id, code=code, **base)
        db.add(row)
        rows.append(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="discount.bulk_create",
        entity="discount",
        after={"count": len(rows), "prefix": prefix, "kind": body.kind.value, "value": body.value},
    )
    return {"items": [await _code_out(db, r) for r in rows], "codes": codes}


AudienceKind = Literal[
    "all", "resellers", "buyers", "active", "inactive", "never_bought", "account_buyers", "panel", "idle"
]
PIN_HOURS_MAX = 24 * 30


class BroadcastBody(BaseModel):
    kind: AudienceKind = "all"
    text: str = Field(min_length=1, max_length=4000)
    buttons: list[dict[str, str]] = Field(default_factory=list, max_length=6)
    resellers_only: bool = False
    buyers_only: bool = False
    locale: str | None = Field(default=None, max_length=8)
    # F-16: start later instead of now (timezone-aware, at most 60 days ahead)
    scheduled_at: datetime | None = None
    # Foxima parity: the buyers of one panel ("panel"), people idle for N days ("idle"), and a pin
    # (0 hours: until unpinned), as the bot offers.
    panel_id: int | None = None
    idle_days: int | None = None
    pin: bool = False
    pin_hours: int = Field(default=0, ge=0, le=PIN_HOURS_MAX)


class TargetedBroadcastBody(BroadcastBody):
    """Explicit contract: an old server must never ignore a new audience kind."""

    kind: AudienceKind = Field(...)
    resellers_only: StrictBool = False
    buyers_only: StrictBool = False


def _check_buttons(body: BroadcastBody) -> None:
    """Link buttons as before, and section buttons that open a part of the bot (Foxima parity)."""
    from paurubot.core.broadcasts import valid_button

    for button in body.buttons:
        if not valid_button(button):
            raise HTTPException(status_code=422, detail="bad_button")


async def _check_audience(db: DB, shop_id: int, body: BroadcastBody) -> dict[str, Any]:
    """The extra audience fields of "panel" and "idle"; refused when they are missing or foreign."""
    from paurubot.core.broadcasts import IDLE_DAYS_MAX
    from paurubot.db.models import Panel

    if body.kind == "panel":
        panel = await db.get(Panel, body.panel_id) if body.panel_id else None
        if panel is None or panel.shop_id != shop_id:
            raise HTTPException(status_code=422, detail="panel_required")
        return {"panel_id": panel.id}
    if body.kind == "idle":
        if not body.idle_days or not 1 <= body.idle_days <= IDLE_DAYS_MAX:
            raise HTTPException(status_code=422, detail="idle_days_invalid")
        return {"idle_days": body.idle_days}
    return {}


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
        **await _check_audience(db, ctx.shop_id, body),
    }
    if body.pin:
        audience.update(pin=True, pin_hours=body.pin_hours)
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


@router.post("/broadcasts/targeted")
async def create_targeted_broadcast(
    body: TargetedBroadcastBody,
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))],
    db: DB,
) -> dict[str, Any]:
    # Legacy mirrors are accepted only when they agree with the explicit kind.
    # Keep /broadcasts unchanged for existing bot/admin clients.
    if (body.resellers_only and body.kind != "resellers") or (body.buyers_only and body.kind != "buyers"):
        raise HTTPException(status_code=422, detail="audience_conflict")
    return await create_broadcast(body, ctx, db)


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


# A broadcast still sending (or paused, to be resumed) is never deleted from under its worker.
FINISHED_BROADCAST = [JobStatus.DONE, JobStatus.CANCELED, JobStatus.FAILED, JobStatus.DEAD]


@router.delete("/broadcasts/{broadcast_id}")
async def delete_broadcast(
    broadcast_id: int, ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, Any]:
    row = await owned(db, Broadcast, broadcast_id, ctx)
    if row.status not in FINISHED_BROADCAST:
        raise HTTPException(status_code=409, detail="broadcast_running")
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.delete",
        entity="broadcast",
        entity_id=broadcast_id,
    )
    await db.commit()
    return {"ok": True}


@router.post("/broadcasts/clear")
async def clear_broadcasts(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, int]:
    """Remove every finished broadcast from the history; running and paused ones stay."""
    rows = list(
        await db.scalars(
            select(Broadcast).where(
                Broadcast.shop_id == ctx.shop_id, Broadcast.status.in_(FINISHED_BROADCAST)
            )
        )
    )
    for row in rows:
        await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="broadcast.clear",
        entity="broadcast",
        after={"deleted": len(rows)},
    )
    await db.commit()
    return {"deleted": len(rows)}


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
    panel_id: int | None = None,
    idle_days: int | None = None,
) -> dict[str, int]:
    from paurubot.db.models import Panel
    from paurubot.notifications.handlers import _audience

    if panel_id is not None:
        panel = await db.get(Panel, panel_id)
        if panel is None or panel.shop_id != ctx.shop_id:
            panel_id = 0  # another shop's panel counts nobody
    draft = Broadcast(
        shop_id=ctx.shop_id,
        audience={
            "kind": kind,
            "locale": locale,
            "resellers_only": resellers_only,
            "buyers_only": buyers_only,
            "panel_id": panel_id,
            "idle_days": idle_days,
        },
    )
    total = await db.scalar(select(func.count(Customer.id)).where(*_audience(draft)))
    return {"reachable": int(total or 0)}


@router.get("/broadcasts/targeted/audience")
async def targeted_audience_size(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))],
    db: DB,
    kind: AudienceKind,
    locale: Annotated[str | None, Query(max_length=8)] = None,
    panel_id: int | None = None,
    idle_days: Annotated[int | None, Query(ge=1, le=365)] = None,
) -> dict[str, int]:
    # Same tenant, opt-out and segment predicates as the legacy count and worker; the panel and
    # idle filters (Foxima parity) ride along only when given.
    extra: dict[str, Any] = {}
    if panel_id is not None:
        extra["panel_id"] = panel_id
    if idle_days is not None:
        extra["idle_days"] = idle_days
    return await audience_size(ctx, db, kind=kind, locale=locale, **extra)


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
    dup = await db.scalar(
        select(GiftCode.id).where(
            GiftCode.shop_id == ctx.shop_id,
            GiftCode.id != code_id,
            func.lower(GiftCode.code) == body.code.lower(),
        )
    )
    if dup:
        raise HTTPException(status_code=409, detail="code_exists")
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


@router.delete("/gift-codes/{code_id}")
async def delete_gift(
    code_id: int, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, bool]:
    if not ctx.can("wallet.adjust"):
        raise HTTPException(status_code=403, detail="forbidden")
    row = await owned(db, GiftCode, code_id, ctx)
    before = snapshot(row)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="gift.delete",
        entity="gift",
        entity_id=code_id,
        before=before,
    )
    return {"ok": True}


class BulkGiftBody(BaseModel):
    count: int = Field(ge=2, le=100)
    prefix: str = Field(default="", max_length=12, pattern=r"^[A-Za-z0-9_-]*$")
    amount: int = Field(ge=1, le=10**10)
    max_uses: int | None = Field(default=1, ge=1)
    new_customers_only: bool = False
    expires_at: datetime | None = None
    is_active: bool = True
    note: str | None = Field(default=None, max_length=255)


@router.post("/gift-codes/bulk")
async def create_gifts_bulk(
    body: BulkGiftBody, ctx: Annotated[StaffContext, Depends(require("discounts.write"))], db: DB
) -> dict[str, Any]:
    if not ctx.can("wallet.adjust"):
        raise HTTPException(status_code=403, detail="forbidden")
    existing = {
        c.lower() for c in await db.scalars(select(GiftCode.code).where(GiftCode.shop_id == ctx.shop_id))
    }
    prefix = body.prefix.upper()
    codes: list[str] = []
    while len(codes) < body.count:
        cand = _random_code(prefix)
        if cand.lower() not in existing:
            existing.add(cand.lower())
            codes.append(cand)
    base = body.model_dump(exclude={"count", "prefix"})
    rows: list[GiftCode] = []
    for code in codes:
        row = GiftCode(shop_id=ctx.shop_id, code=code, **base)
        db.add(row)
        rows.append(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="gift.bulk_create",
        entity="gift",
        after={"count": len(rows), "prefix": prefix, "amount": body.amount},
    )
    return {"items": [plain(r, GIFT_FIELDS) for r in rows], "codes": codes}


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
                "customer": c.staff_label,
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
    "pick",
    "points_per",
    "board",
    "prizes",
    "starts_at",
    "ends_at",
    "status",
    "result",
]


class LotteryPrize(BaseModel):
    kind: Literal["service", "wallet", "text"]
    value: int = Field(default=0, ge=0, le=10**10)  # GB for a new service, toman for credit
    days: int = Field(default=0, ge=0, le=3650)
    panel_id: int = Field(default=0, ge=0)
    label: str = Field(default="", max_length=64)


class LotteryBody(BaseModel):
    title: str = Field(min_length=1, max_length=96)
    prize: str = Field(default="", max_length=255)
    prize_amount: int = Field(default=0, ge=0, le=10**10)
    winners: int = Field(default=1, ge=1, le=100)
    mode: str = Field(default="join", pattern=r"^(join|purchases|points)$")
    pick: str = Field(default="random", pattern=r"^(random|top)$")
    points_per: int = Field(default=0, ge=0, le=10**9)
    board: str = Field(default="none", pattern=r"^(none|self|top)$")
    prizes: list[LotteryPrize] = Field(default_factory=list, max_length=20)
    starts_at: datetime | None = None
    ends_at: datetime


class LotteryPrizes(BaseModel):
    prizes: list[LotteryPrize] = Field(max_length=20)


async def _clean_prizes(db: Any, shop_id: int, items: list[LotteryPrize]) -> list[dict[str, Any]]:
    """Valid prizes whose new services go on this shop's own active panels."""
    from paurubot.core import growth

    try:
        prizes = growth.validate_lottery_prizes([item.model_dump() for item in items])
        await growth.check_prize_panels(db, shop_id, prizes)
    except growth.GrowthError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from exc
    return prizes


def _contact(ctx: StaffContext, customer: Any) -> dict[str, Any]:
    """Who a winner or player is, for staff who may read customers (an API key may not)."""
    if customer is None or not ctx.can("customers.read"):
        return {"username": None, "telegram_id": None, "phone": None}
    return {"username": customer.username, "telegram_id": customer.telegram_id, "phone": customer.phone}


@router.get("/lotteries")
async def list_lotteries(ctx: Sender, db: DB) -> dict[str, Any]:
    from paurubot.core import growth
    from paurubot.db.models import Customer, Lottery
    from paurubot.notifications import log_reports

    rows = list(
        await db.scalars(
            select(Lottery).where(Lottery.shop_id == ctx.shop_id).order_by(Lottery.id.desc()).limit(50)
        )
    )
    items = []
    for row in rows:
        data = plain(row, LOTTERY_FIELDS)
        data["prizes"] = growth.prize_list(row)
        if row.status == "drawn":
            result = row.result or {}
            data["entrants"], data["tickets"] = (
                int(result.get("entrants") or 0),
                int(result.get("tickets") or 0),
            )
        else:
            pool = await growth.tickets(db, row)
            data["entrants"], data["tickets"] = len(pool), sum(pool.values())
        names, awards = [], []
        for award in await log_reports.current_awards(db, row):
            customer = await db.get(Customer, int(award["customer_id"]))
            name = log_reports.person_name(customer) if customer else f"#{award['customer_id']}"
            names.append({"id": int(award["customer_id"]), "name": name})
            awards.append({**award, "name": name, **_contact(ctx, customer)})
        data["winner_names"], data["awards"] = names, awards
        items.append(data)
    panels = [{"id": p.id, "name": p.name} for p in await growth.prize_panels(db, ctx.shop_id)]
    return {"items": items, "panels": panels}


@router.get("/lotteries/{lottery_id}/standings")
async def lottery_standings(
    lottery_id: int,
    ctx: Sender,
    db: DB,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Everyone with a score, best first, with who they are (GRW-04)."""
    from paurubot.core import growth
    from paurubot.db.models import Customer, Lottery
    from paurubot.notifications import log_reports

    row = await owned(db, Lottery, lottery_id, ctx)
    board = await growth.standings(db, row)
    items = []
    for rank, entry in enumerate(board[offset : offset + limit], start=offset + 1):
        customer = await db.get(Customer, entry.customer_id)
        if customer is None:
            continue
        items.append(
            {
                "rank": rank,
                "customer_id": customer.id,
                "name": log_reports.person_name(customer),
                **_contact(ctx, customer),
                "points": entry.points,
                "purchases": entry.purchases,
                "spent": entry.spent,
            }
        )
    return {"total": len(board), "items": items}


@router.post("/lotteries")
async def create_lottery(body: LotteryBody, ctx: Sender, db: DB) -> dict[str, Any]:
    from paurubot.db.base import utcnow
    from paurubot.db.models import Lottery

    starts = body.starts_at or utcnow()
    if body.ends_at <= starts or body.ends_at <= utcnow():
        raise HTTPException(status_code=422, detail="lottery_dates_invalid")
    prizes = await _clean_prizes(db, ctx.shop_id, body.prizes)
    if not prizes and not body.prize.strip():
        raise HTTPException(status_code=422, detail="lottery_prizes_invalid")
    if body.mode == "points" and body.points_per < 1:
        raise HTTPException(status_code=422, detail="lottery_points_per_invalid")
    values = body.model_dump()
    values.update(
        starts_at=starts,
        prizes=prizes,
        winners=len(prizes) or body.winners,
        points_per=body.points_per if body.mode == "points" else 0,
    )
    row = Lottery(shop_id=ctx.shop_id, **values)
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


@router.put("/lotteries/{lottery_id}/prizes")
async def set_lottery_prizes(lottery_id: int, body: LotteryPrizes, ctx: Sender, db: DB) -> dict[str, Any]:
    """Change an open lottery's prizes, first place first."""
    from paurubot.db.models import Lottery

    row = await owned(db, Lottery, lottery_id, ctx)
    if row.status != "open":
        raise HTTPException(status_code=409, detail="lottery_closed")
    prizes = await _clean_prizes(db, ctx.shop_id, body.prizes)
    if not prizes:
        raise HTTPException(status_code=422, detail="lottery_prizes_invalid")
    before = {"prizes": row.prizes}
    row.prizes, row.winners, row.prize, row.prize_amount = prizes, len(prizes), "", 0
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="lottery.prizes",
        entity="lottery",
        entity_id=row.id,
        before=before,
        after={"prizes": prizes},
    )
    return plain(row, LOTTERY_FIELDS)


@router.post("/lotteries/{lottery_id}/{action}")
async def lottery_action(lottery_id: int, action: str, ctx: Sender, db: DB) -> dict[str, Any]:
    """`draw` now (instead of waiting for the end time) or `cancel` an open lottery."""
    from paurubot.core import growth
    from paurubot.db.models import Lottery

    row = await db.scalar(
        select(Lottery).where(Lottery.id == lottery_id, Lottery.shop_id == ctx.shop_id).with_for_update()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if row.status != "open":
        raise HTTPException(status_code=409, detail="lottery_closed")
    if action == "cancel":
        row.status = "canceled"
    elif action == "draw":
        if not growth.prize_list(row):
            raise HTTPException(status_code=422, detail="lottery_prizes_invalid")
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


# ---------------------------------------------------------------- referral leaderboard


@router.get("/referrals/leaderboard")
async def referral_leaderboard(
    ctx: Annotated[StaffContext, Depends(require("customers.read"))],
    db: DB,
    days: int = Query(default=0, ge=0, le=365),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Top referrers by invited friends, qualified purchases, commission earned, and free configs."""
    from paurubot.db.base import utcnow
    from paurubot.db.models import (
        LedgerAccount,
        LedgerAccountKind,
        LedgerEntry,
        LedgerJournal,
        Order,
        OrderStatus,
        ReferralClaim,
    )

    since = utcnow() - timedelta(days=days) if days > 0 else None

    inv_conds = [Customer.shop_id == ctx.shop_id, Customer.referrer_id.is_not(None)]
    if since is not None:
        inv_conds.append(Customer.created_at >= since)
    inv_rows = (
        await db.execute(
            select(Customer.referrer_id, func.count(Customer.id))
            .where(*inv_conds)
            .group_by(Customer.referrer_id)
        )
    ).all()
    invited: dict[int, int] = {int(rid): int(cnt) for rid, cnt in inv_rows if rid is not None}

    qual_conds = [
        Customer.shop_id == ctx.shop_id,
        Customer.referrer_id.is_not(None),
        Customer.is_banned.is_(False),
        Order.status == OrderStatus.COMPLETED,
        Order.amount > 0,
    ]
    if since is not None:
        qual_conds.append(Order.created_at >= since)
    qual_rows = (
        await db.execute(
            select(Customer.referrer_id, func.count(func.distinct(Customer.id)))
            .join(Order, Order.customer_id == Customer.id)
            .where(*qual_conds)
            .group_by(Customer.referrer_id)
        )
    ).all()
    qualified: dict[int, int] = {int(rid): int(cnt) for rid, cnt in qual_rows if rid is not None}

    comm_conds = [
        LedgerAccount.shop_id == ctx.shop_id,
        LedgerAccount.kind == LedgerAccountKind.WALLET,
        LedgerAccount.customer_id.is_not(None),
        LedgerEntry.amount > 0,
        LedgerJournal.idempotency_key.like("referral:%"),
    ]
    if since is not None:
        comm_conds.append(LedgerJournal.created_at >= since)
    comm_rows = (
        await db.execute(
            select(LedgerAccount.customer_id, func.coalesce(func.sum(LedgerEntry.amount), 0))
            .join(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .join(LedgerJournal, LedgerJournal.id == LedgerEntry.journal_id)
            .where(*comm_conds)
            .group_by(LedgerAccount.customer_id)
        )
    ).all()
    commission: dict[int, int] = {int(cid): int(amt) for cid, amt in comm_rows if cid is not None}

    claim_conds = [ReferralClaim.shop_id == ctx.shop_id]
    if since is not None:
        claim_conds.append(ReferralClaim.created_at >= since)
    claim_rows = (
        await db.execute(
            select(ReferralClaim.customer_id, func.count(ReferralClaim.id))
            .where(*claim_conds)
            .group_by(ReferralClaim.customer_id)
        )
    ).all()
    claims: dict[int, int] = {int(cid): int(cnt) for cid, cnt in claim_rows if cid is not None}

    all_ids = set(invited) | set(qualified) | set(commission) | set(claims)
    ranked_ids = sorted(
        all_ids,
        key=lambda cid: (
            -invited.get(cid, 0),
            -commission.get(cid, 0),
            -qualified.get(cid, 0),
            cid,
        ),
    )[:limit]
    people = (
        {
            c.id: c
            for c in await db.scalars(
                select(Customer).where(Customer.shop_id == ctx.shop_id, Customer.id.in_(ranked_ids))
            )
        }
        if ranked_ids
        else {}
    )
    items = []
    for rank, cid in enumerate(ranked_ids, start=1):
        person = people.get(cid)
        if person is None:
            continue
        items.append(
            {
                "rank": rank,
                "customer_id": cid,
                "name": person.display_name,
                "username": person.username,
                "telegram_id": person.telegram_id,
                "invited": invited.get(cid, 0),
                "qualified": qualified.get(cid, 0),
                "commission": commission.get(cid, 0),
                "config_claims": claims.get(cid, 0),
            }
        )
    return {
        "summary": {
            "total_invited": sum(invited.values()),
            "total_qualified": sum(qualified.values()),
            "total_commission": sum(commission.values()),
            "total_config_claims": sum(claims.values()),
        },
        "items": items,
    }
