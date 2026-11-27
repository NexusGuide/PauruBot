"""Staff tools the bot's admin menu had and the web panel lacked: a wallet gift for an audience,
a new trial round for everyone, plan prices up or down at once, the reseller ranking, and taking
every broadcast pin down. Each runs the same core code as the bot and is audited the same way."""

from __future__ import annotations

import secrets
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import pricing, trials
from paurubot.db.models import Customer, Plan
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin/tools", tags=["admin"])

Audience = Literal["all", "buyers", "active", "inactive", "never_bought", "resellers", "account_buyers"]
GIFT_MIN, GIFT_MAX = 1_000, 10_000_000  # as in the bot's gift (ADM-09)


def _shop_wide(ctx: StaffContext) -> None:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")


@router.get("/gift")
async def gift_audience(
    ctx: Annotated[StaffContext, Depends(require("wallet.adjust"))], db: DB, audience: Audience = "all"
) -> dict[str, int]:
    """How many customers a gift for this audience would reach."""
    from paurubot.notifications.handlers import audience_conditions

    _shop_wide(ctx)
    count = await db.scalar(
        select(func.count(Customer.id)).where(*audience_conditions(ctx.shop_id, audience))
    )
    return {"count": int(count or 0)}


class GiftBody(BaseModel):
    amount: int = Field(ge=GIFT_MIN, le=GIFT_MAX)
    audience: Audience


@router.post("/gift")
async def gift(
    body: GiftBody, ctx: Annotated[StaffContext, Depends(require("wallet.adjust"))], db: DB
) -> dict[str, Any]:
    """The same wallet gift for everyone in an audience; credited in the background, never twice."""
    _shop_wide(ctx)
    batch = secrets.token_hex(6)
    await queue.enqueue(
        db,
        "broadcast",
        "gift",
        {"batch": batch, "amount": body.amount, "audience": body.audience, "cursor": 0},
        shop_id=ctx.shop_id,
        dedupe_key=f"gift:{batch}",
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="wallet.gift_all",
        entity="shop",
        entity_id=ctx.shop_id,
        after={"amount": body.amount, "audience": body.audience, "batch": batch},
    )
    await db.commit()
    return {"batch": batch}


class TrialResetBody(BaseModel):
    scope: Literal["all", "non_buyers"] = "all"


@router.get("/trial-reset/preview")
async def trial_reset_preview(
    ctx: Annotated[StaffContext, Depends(require("settings.write"))], db: DB
) -> dict[str, int]:
    """Preview how many trial users would be reset under each scope."""
    _shop_wide(ctx)
    return await trials.reset_preview(db, ctx.shop_id)


@router.post("/trial-reset")
async def trial_reset(
    ctx: Annotated[StaffContext, Depends(require("settings.write"))],
    db: DB,
    body: TrialResetBody | None = None,
) -> dict[str, Any]:
    """Start a new trial round for everyone, or reset only eligible non-buyers."""
    _shop_wide(ctx)
    scope = body.scope if body is not None else "all"
    if scope == "non_buyers":
        reset_count, generation = await trials.reset_non_buyers(db, ctx.shop_id)
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="trial.reset_non_buyers",
            entity="shop",
            entity_id=ctx.shop_id,
            after={"scope": scope, "reset_count": reset_count, "trial_generation": generation},
        )
        await db.commit()
        return {"generation": generation, "scope": scope, "reset_count": reset_count}
    generation = await trials.new_round(db, ctx.shop_id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="settings.update",
        entity="shop",
        entity_id=ctx.shop_id,
        after={"trial_generation": generation, "scope": scope},
    )
    await db.commit()
    return {"generation": generation, "scope": scope}


class PricesBody(BaseModel):
    percent: int = Field(ge=-90, le=90)


@router.post("/plan-prices")
async def plan_prices(
    body: PricesBody, ctx: Annotated[StaffContext, Depends(require("catalog.write"))], db: DB
) -> dict[str, int]:
    """Every active plan's price up or down by a percentage, rounded like other prices."""
    _shop_wide(ctx)
    if body.percent == 0:
        raise HTTPException(status_code=422, detail="percent_zero")
    plans = list(
        await db.scalars(
            select(Plan).where(Plan.shop_id == ctx.shop_id, Plan.is_active.is_(True)).with_for_update()
        )
    )
    for plan in plans:
        plan.price = pricing.round_price(plan.price * (100 + body.percent) / 100)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="plan.bulk_price",
        entity="plan",
        after={"percent": body.percent, "plans": len(plans)},
    )
    await db.commit()
    return {"plans": len(plans)}


@router.get("/reseller-ranking")
async def reseller_ranking(
    ctx: Annotated[StaffContext, Depends(require("resellers.manage"))], db: DB, days: int = 30
) -> dict[str, Any]:
    """Resellers by sales, revenue and confirmed complaints over the last `days` (0: all time)."""
    from paurubot.core.reseller_ranking import rankings

    _shop_wide(ctx)
    rows = await rankings(db, ctx.shop_id, days=max(0, min(days, 365)) or None)
    return {
        "items": [
            {
                "rank": row.rank,
                "profile_id": row.profile_id,
                "name": row.name,
                "score": row.score,
                "sales": row.sales,
                "revenue": row.revenue,
                "complaints": row.complaints,
            }
            for row in rows
        ]
    }


@router.post("/unpin-all")
async def unpin_all(
    ctx: Annotated[StaffContext, Depends(require("broadcast.send"))], db: DB
) -> dict[str, bool]:
    """Take down every broadcast still pinned in customers' chats (GRW-06)."""
    _shop_wide(ctx)
    await queue.enqueue(
        db,
        "broadcast",
        "unpin",
        {"all": True},
        shop_id=ctx.shop_id,
        dedupe_key=f"unpin:{ctx.shop_id}:{secrets.token_hex(4)}",
    )
    await record(db, shop_id=ctx.shop_id, actor=ctx.actor, action="broadcast.unpin_all", entity="shop")
    await db.commit()
    return {"ok": True}


ServiceGiftMode = Literal["days", "gb"]


def _check_service_gift_value(mode: ServiceGiftMode, value: int) -> None:
    limit = 3650 if mode == "days" else 100_000
    if not 1 <= value <= limit:
        raise HTTPException(status_code=422, detail="invalid_gift_value")


async def _check_panel_filter(db: DB, shop_id: int, panel_id: int | None) -> int | None:
    if panel_id is None:
        return None
    from paurubot.db.models import Panel

    found = await db.scalar(select(Panel.id).where(Panel.id == panel_id, Panel.shop_id == shop_id))
    if found is None:
        raise HTTPException(status_code=404, detail="panel_not_found")
    return panel_id


@router.get("/service-gift")
async def service_gift_preview(
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
    mode: ServiceGiftMode = "days",
    value: int = 1,
    panel_id: int | None = None,
) -> dict[str, int]:
    """How many active services a mass days/GB gift would update."""
    from paurubot.core import service_gifts

    _shop_wide(ctx)
    _check_service_gift_value(mode, value)
    pid = await _check_panel_filter(db, ctx.shop_id, panel_id)
    try:
        targets = await service_gifts.preview(db, ctx.shop_id, mode, value, panel_id=pid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="too_many_services") from exc
    return {"count": len(targets), "limit": service_gifts.LIMIT}


class ServiceGiftBody(BaseModel):
    mode: ServiceGiftMode
    value: int = Field(ge=1, le=100_000)
    panel_id: int | None = None


@router.post("/service-gift")
async def service_gift(
    body: ServiceGiftBody, ctx: Annotated[StaffContext, Depends(require("services.write"))], db: DB
) -> dict[str, Any]:
    """Add days or GB to every active service (or one panel's services) in the background."""
    from paurubot.core import service_gifts

    _shop_wide(ctx)
    _check_service_gift_value(body.mode, body.value)
    pid = await _check_panel_filter(db, ctx.shop_id, body.panel_id)
    try:
        targets = await service_gifts.preview(db, ctx.shop_id, body.mode, body.value, panel_id=pid)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="too_many_services") from exc
    batch = f"web:{secrets.token_hex(6)}"
    queued = 0
    for target in targets:
        job_id = await queue.enqueue(
            db,
            "bulk",
            "service",
            {
                "service_id": target["service_id"],
                "action": "extend",
                "gift_target": target,
                "staff_id": ctx.staff.id,
                "batch": batch,
            },
            shop_id=ctx.shop_id,
            dedupe_key=f"bulk:{batch}:{target['service_id']}",
            max_attempts=5,
        )
        queued += job_id is not None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.bulk_extend",
        entity="service",
        after={
            "source": "web",
            "count": queued,
            "batch": batch,
            "mode": body.mode,
            "value": body.value,
            "panel_id": pid,
        },
    )
    await db.commit()
    return {"queued": queued, "batch": batch}
