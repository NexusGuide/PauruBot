"""A reseller's own bot, managed by the reseller ("my bot") and watched by the owner (TEN-04)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.api.admin_common import owned, reseller_profile
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import resale
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, ResellerProfile
from paurubot.ledger.accounts import wallet_balance

router = APIRouter(prefix="/api/admin", tags=["admin"])
OwnBot = Annotated[StaffContext, Depends(require("resellers.bot"))]
Manager = Annotated[StaffContext, Depends(require("resellers.manage"))]


async def _view(db: AsyncSession, profile: ResellerProfile) -> dict[str, Any]:
    shop = await resale.shop_of(db, profile)
    reseller = await db.get(Customer, profile.customer_id)
    balance = await wallet_balance(db, profile.shop_id, profile.customer_id) if reseller else 0
    data: dict[str, Any] = {
        "connected": False,
        "balance": balance,
        "discount_percent": profile.discount_percent,
        "metered": profile.is_metered,
    }
    if shop is None:
        return data
    cfg = await shop_settings.load(db, shop.id, fresh=True)
    numbers = await resale.figures(db, shop)
    data.update(
        connected=shop.bot_token_enc is not None,
        blocked=bool(cfg.get("resale_blocked")),
        shop_id=shop.id,
        title=shop.title,
        bot_username=shop.bot_username,
        status=shop.status.value,
        settings={key: shop_settings.public_view(cfg)[key] for key in sorted(resale.EDITABLE)},
        customers=numbers.customers,
        active_services=numbers.active,
        orders_month=numbers.orders_month,
        revenue_month=numbers.revenue_month,
    )
    return data


async def _own_profile(db: AsyncSession, ctx: StaffContext) -> ResellerProfile:
    profile = await reseller_profile(db, ctx)
    if profile is None:
        raise HTTPException(status_code=403, detail="forbidden")
    return profile


@router.get("/my-bot")
async def my_bot(ctx: OwnBot, db: DB) -> dict[str, Any]:
    return await _view(db, await _own_profile(db, ctx))


class BotBody(BaseModel):
    token: str | None = Field(default=None, max_length=100)
    title: str = Field(default="", max_length=96)
    settings: dict[str, Any] = Field(default_factory=dict)


@router.put("/my-bot")
async def save_my_bot(body: BotBody, ctx: OwnBot, db: DB) -> dict[str, Any]:
    profile = await _own_profile(db, ctx)
    if profile.is_metered:
        raise HTTPException(status_code=422, detail="reseller_bot_needs_prepaid")
    shop = await resale.shop_of(db, profile)
    connected_now = False
    try:
        if body.token:
            shop = await resale.connect(db, profile, body.token, body.title)
            connected_now = True
        elif shop is not None and body.title.strip():
            shop.title = body.title.strip()[:96]
        if body.settings:
            if shop is None:
                raise HTTPException(status_code=422, detail="reseller_bot_not_connected")
            if "card_accounts" in body.settings:
                from paurubot.api.routes.admin.system import _check_card_accounts

                _check_card_accounts(body.settings["card_accounts"])
            await resale.update(db, shop, body.settings)
    except resale.ResaleError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller_bot.connected" if connected_now else "reseller_bot.updated",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"bot": shop.bot_username if shop else None, "settings": sorted(body.settings)},
    )
    await db.commit()
    if connected_now and shop is not None:
        try:
            await resale.point_webhook(shop.id)
        except Exception:  # Telegram can be retried; the bot is saved either way
            pass
    return await _view(db, profile)


@router.delete("/my-bot")
async def stop_my_bot(ctx: OwnBot, db: DB) -> dict[str, Any]:
    profile = await _own_profile(db, ctx)
    shop = await resale.disconnect(db, profile)
    if shop is not None:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="reseller_bot.stopped",
            entity="reseller_profile",
            entity_id=profile.id,
        )
    await db.commit()
    return await _view(db, profile)


@router.get("/resellers/{profile_id}/bot")
async def reseller_bot(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    profile = await owned(db, ResellerProfile, profile_id, ctx)
    return await _view(db, profile)


@router.post("/resellers/{profile_id}/bot/stop")
async def stop_reseller_bot(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    profile = await owned(db, ResellerProfile, profile_id, ctx)
    shop = await resale.disconnect(db, profile, by_staff=True)
    if shop is not None:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="reseller_bot.stopped_by_staff",
            entity="reseller_profile",
            entity_id=profile.id,
        )
    await db.commit()
    return await _view(db, profile)


@router.post("/resellers/{profile_id}/bot/allow")
async def allow_reseller_bot(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    profile = await owned(db, ResellerProfile, profile_id, ctx)
    if await resale.allow(db, profile) is not None:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="reseller_bot.allowed",
            entity="reseller_profile",
            entity_id=profile.id,
        )
    await db.commit()
    return await _view(db, profile)


async def _webhook_check(db: DB, profile: ResellerProfile) -> dict[str, Any]:
    shop = await resale.shop_of(db, profile)
    if shop is None or shop.bot_token_enc is None:
        raise HTTPException(status_code=422, detail="reseller_bot_not_connected")
    return await resale.webhook_status(shop.id)


async def _webhook_repoint(db: DB, ctx: StaffContext, profile: ResellerProfile) -> dict[str, Any]:
    shop = await resale.shop_of(db, profile)
    if shop is None or shop.bot_token_enc is None:
        raise HTTPException(status_code=422, detail="reseller_bot_not_connected")
    try:
        await resale.point_webhook(shop.id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller_bot.webhook_repoint",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"shop_id": shop.id},
    )
    await db.commit()
    return await resale.webhook_status(shop.id)


@router.get("/my-bot/webhook")
async def my_bot_webhook(ctx: OwnBot, db: DB) -> dict[str, Any]:
    return await _webhook_check(db, await _own_profile(db, ctx))


@router.post("/my-bot/webhook/repoint")
async def repoint_my_bot_webhook(ctx: OwnBot, db: DB) -> dict[str, Any]:
    return await _webhook_repoint(db, ctx, await _own_profile(db, ctx))


@router.get("/resellers/{profile_id}/bot/webhook")
async def reseller_bot_webhook(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    return await _webhook_check(db, await owned(db, ResellerProfile, profile_id, ctx))


@router.post("/resellers/{profile_id}/bot/webhook/repoint")
async def repoint_reseller_bot_webhook(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    return await _webhook_repoint(db, ctx, await owned(db, ResellerProfile, profile_id, ctx))


@router.get("/my-bot/logs")
async def my_bot_logs(
    ctx: OwnBot,
    db: DB,
    category: Literal["all", "errors", "orders", "payments", "jobs", "audit"] = "all",
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=10, le=300),
) -> dict[str, Any]:
    """Activity, order, payment and queue logs for the reseller's own dedicated bot."""
    profile = await _own_profile(db, ctx)
    shop = await resale.shop_of(db, profile)
    if shop is None:
        raise HTTPException(status_code=422, detail="reseller_bot_not_connected")
    return await resale.bot_logs(db, shop, category=category, q=q, limit=limit, staff=False)


@router.get("/my-bot/logs.csv")
async def my_bot_logs_csv(
    ctx: OwnBot,
    db: DB,
    category: Literal["all", "errors", "orders", "payments", "jobs", "audit"] = "all",
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=300, ge=10, le=300),
) -> Response:
    """Export the reseller's own bot logs as CSV."""
    profile = await _own_profile(db, ctx)
    shop = await resale.shop_of(db, profile)
    if shop is None:
        raise HTTPException(status_code=422, detail="reseller_bot_not_connected")
    data = await resale.bot_logs(db, shop, category=category, q=q, limit=limit, staff=False)
    return Response(
        content=resale.bot_logs_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="my-bot-{shop.id}-logs.csv"'},
    )
