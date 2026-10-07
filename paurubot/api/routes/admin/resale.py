"""A reseller's own bot, managed by the reseller ("my bot") and watched by the owner (TEN-04)."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.api.admin_common import owned, reseller_profile
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import resale
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Order, OrderStatus, ResellerProfile, Service, ServiceStatus
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
    month = utcnow() - timedelta(days=30)
    sold = await db.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount), 0)).where(
            Order.shop_id == shop.id, Order.status == OrderStatus.COMPLETED, Order.completed_at >= month
        )
    )
    orders, revenue = sold.one()
    data.update(
        connected=shop.bot_token_enc is not None,
        blocked=bool(cfg.get("resale_blocked")),
        shop_id=shop.id,
        title=shop.title,
        bot_username=shop.bot_username,
        status=shop.status.value,
        settings={key: shop_settings.public_view(cfg)[key] for key in sorted(resale.EDITABLE)},
        customers=int(
            await db.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id)) or 0
        ),
        active_services=int(
            await db.scalar(
                select(func.count(Service.id)).where(
                    Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE
                )
            )
            or 0
        ),
        orders_month=int(orders or 0),
        revenue_month=int(revenue or 0),
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
