"""A customer's dedicated reseller bot on its own panel (OPB): connect, follow, pause, stop."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.admin_common import owned
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import own_panel
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, PanelAccount, Service, ServiceStatus, Shop, ShopStatus

router = APIRouter(prefix="/api/admin", tags=["admin"])
Manager = Annotated[StaffContext, Depends(require("resellers.manage"))]


def _account(account: PanelAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "username": account.username,
        "status": account.status,
        "plan_title": account.plan.title if account.plan is not None else None,
        "data_limit": account.data_limit,
        "used_bytes": account.used_bytes,
        "max_users": account.max_users,
        "users_count": account.users_count,
        "expire_at": account.expire_at.isoformat() if account.expire_at else None,
        "usable": own_panel.usable(account),
    }


async def _bot(db: DB, shop: Shop | None) -> dict[str, Any] | None:
    if shop is None:
        return None
    cfg = await shop_settings.load(db, shop.id, fresh=True)
    customers = await db.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id))
    active = await db.scalar(
        select(func.count(Service.id)).where(
            Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE
        )
    )
    return {
        "shop_id": shop.id,
        "title": shop.title,
        "bot_username": shop.bot_username,
        "connected": shop.status == ShopStatus.ACTIVE and shop.bot_token_enc is not None,
        "account_id": shop.own_account_id,
        "paused": str(cfg.get(own_panel.PAUSED) or ""),
        "hold": bool(cfg.get(own_panel.HOLD)),
        "trial_enabled": bool(cfg.get("trial_enabled")),
        "customers": int(customers or 0),
        "active_services": int(active or 0),
    }


async def _customer(db: DB, ctx: StaffContext, customer_id: int) -> Customer:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    return await owned(db, Customer, customer_id, ctx)


@router.get("/customers/{customer_id}/own-bot")
async def own_bot(customer_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    customer = await _customer(db, ctx, customer_id)
    accounts = await own_panel.eligible_accounts(db, customer)
    return {
        "accounts": [_account(a) for a in accounts],
        "bot": await _bot(db, await own_panel.shop_for_customer(db, customer)),
    }


class ConnectBody(BaseModel):
    account_id: int
    # Needed the first time; left out, the bot keeps its token and only follows the new account.
    token: str | None = Field(default=None, max_length=128)
    title: str = Field(default="", max_length=96)


@router.post("/customers/{customer_id}/own-bot")
async def connect_own_bot(customer_id: int, body: ConnectBody, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.security import secretbox

    customer = await _customer(db, ctx, customer_id)
    account = await db.get(PanelAccount, body.account_id)
    if account is None or account.customer_id != customer.id or account.shop_id != ctx.shop_id:
        raise HTTPException(status_code=404, detail="not_found")
    shop = await own_panel.shop_for_customer(db, customer)
    token = (body.token or "").strip()
    if not token:
        if shop is None or shop.bot_token_enc is None:
            raise HTTPException(status_code=422, detail="bot_token_invalid")
        token = secretbox.decrypt("bot-token", shop.bot_token_enc)
    try:
        shop = await own_panel.connect(db, account, token, body.title)
    except own_panel.OwnPanelError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=exc.key) from None
    await own_panel.announce(db, [(shop, alert)] if (alert := await own_panel.recheck(db, shop)) else [])
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.connect",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id, "account_id": account.id, "bot": shop.bot_username},
    )
    await db.commit()
    from paurubot.core import resale

    try:
        await resale.point_webhook(shop.id)
    except Exception:  # the periodic start-up pass points it again; never fail the save for it
        pass
    return {"bot": await _bot(db, shop)}


class HoldBody(BaseModel):
    hold: bool


@router.post("/customers/{customer_id}/own-bot/hold")
async def hold_own_bot(customer_id: int, body: HoldBody, ctx: Manager, db: DB) -> dict[str, Any]:
    customer = await _customer(db, ctx, customer_id)
    shop = await own_panel.shop_for_customer(db, customer)
    if shop is None:
        raise HTTPException(status_code=404, detail="not_found")
    await shop_settings.set_many(db, shop.id, {own_panel.HOLD: body.hold})
    paused_anyway = bool((await shop_settings.load(db, shop.id, fresh=True)).get(own_panel.PAUSED))
    if body.hold or not paused_anyway:  # released while the panel itself still can't serve: quiet
        await own_panel.announce(db, [(shop, "own_paused_hold" if body.hold else "own_resumed")])
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.hold" if body.hold else "own_bot.release",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id},
    )
    await db.commit()
    return {"bot": await _bot(db, shop)}


@router.delete("/customers/{customer_id}/own-bot")
async def disconnect_own_bot(customer_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    customer = await _customer(db, ctx, customer_id)
    shop = await own_panel.shop_for_customer(db, customer)
    if shop is None:
        raise HTTPException(status_code=404, detail="not_found")
    await own_panel.disconnect(db, shop)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.disconnect",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id},
    )
    await db.commit()
    return {"bot": await _bot(db, shop)}
