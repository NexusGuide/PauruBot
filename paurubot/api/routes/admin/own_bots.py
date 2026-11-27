"""A customer's dedicated reseller bot on its own panel (OPB): connect, follow, pause, stop."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.admin_common import owned
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record
from paurubot.core import own_panel
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Customer,
    PanelAccount,
    ResellerProfile,
    Service,
    ServiceStatus,
    Shop,
    ShopStatus,
)

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
        "expires_at": cfg.get(own_panel.EXPIRES) or None,
        "customers": int(customers or 0),
        "active_services": int(active or 0),
        "admins": await own_panel.admins(db, shop),
        "max_admins": own_panel.MAX_ADMINS,
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


def _state(shop: Shop, cfg: dict[str, Any]) -> str:
    """One word for the list: blocked|stopped|held|paused|selling."""
    if cfg.get("resale_blocked"):
        return "blocked"  # stopped by the shop's staff; only they let it connect again
    if shop.status != ShopStatus.ACTIVE or shop.bot_token_enc is None:
        return "stopped"
    if cfg.get(own_panel.HOLD):
        return "held"
    if cfg.get(own_panel.PAUSED):
        return "paused"
    return "selling"


@router.get("/customer-bots")
async def customer_bots(ctx: Manager, db: DB) -> dict[str, Any]:
    """Every bot the shop's customers run: resellers' bots selling the shop's plans (TEN-04) and
    dedicated bots on a customer's own panel (OPB), with the figures to follow them."""
    from paurubot.core import resale

    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    shops = list(
        await db.scalars(
            select(Shop)
            .where(
                Shop.parent_id == ctx.shop_id,
                (Shop.reseller_profile_id.is_not(None)) | (Shop.own_account_id.is_not(None)),
            )
            .order_by(Shop.id.desc())
        )
    )
    items: list[dict[str, Any]] = []
    for shop in shops:
        cfg = await shop_settings.load(db, shop.id, fresh=True)
        profile = (
            await db.get(ResellerProfile, shop.reseller_profile_id) if shop.reseller_profile_id else None
        )
        account = await db.get(PanelAccount, shop.own_account_id) if shop.own_account_id else None
        owner_id = profile.customer_id if profile else account.customer_id if account else None
        owner = await db.get(Customer, owner_id) if owner_id else None
        numbers = await resale.figures(db, shop)
        items.append(
            {
                "shop_id": shop.id,
                "kind": "own" if shop.own_account_id else "reseller",
                "title": shop.title,
                "bot_username": shop.bot_username,
                "state": _state(shop, cfg),
                "paused": str(cfg.get(own_panel.PAUSED) or ""),
                "expires_at": cfg.get(own_panel.EXPIRES) or None,
                "owner": {"id": owner.id, "name": owner.staff_label, "username": owner.username}
                if owner
                else None,
                "profile_id": profile.id if profile else None,
                "account": {"id": account.id, "username": account.username, "status": account.status}
                if account
                else None,
                "customers": numbers.customers,
                "active_services": numbers.active,
                "orders_month": numbers.orders_month,
                "revenue_month": numbers.revenue_month,
                "created_at": shop.created_at.isoformat(),
            }
        )
    return {"items": items}


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


class ExpiryBody(BaseModel):
    # Days added to the bot's end (from now once it has ended); null: no end at all.
    days: int | None = Field(default=None, ge=1, le=own_panel.MAX_DAYS)


@router.post("/customers/{customer_id}/own-bot/expiry")
async def own_bot_expiry(customer_id: int, body: ExpiryBody, ctx: Manager, db: DB) -> dict[str, Any]:
    """How long the main shop lets this bot sell; at the end it stops selling, as on a pause."""
    customer = await _customer(db, ctx, customer_id)
    shop = await own_panel.shop_for_customer(db, customer)
    if shop is None:
        raise HTTPException(status_code=404, detail="not_found")
    end = await own_panel.set_expiry(db, shop, body.days)
    await own_panel.announce(db, [(shop, alert)] if (alert := await own_panel.recheck(db, shop)) else [])
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.expiry",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id, "expires_at": end.isoformat() if end else None},
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


class AdminBody(BaseModel):
    telegram_id: int = Field(gt=0, le=10**13)


_ADMIN_STATUS = {"own_admin_exists": 409, "own_admin_not_found": 404}


async def _bot_shop(db: DB, ctx: StaffContext, customer_id: int) -> tuple[Customer, Shop]:
    customer = await _customer(db, ctx, customer_id)
    shop = await own_panel.shop_for_customer(db, customer)
    if shop is None:
        raise HTTPException(status_code=404, detail="not_found")
    return customer, shop


@router.post("/customers/{customer_id}/own-bot/admins")
async def add_own_bot_admin(customer_id: int, body: AdminBody, ctx: Manager, db: DB) -> dict[str, Any]:
    """Let one more Telegram account run the customer's bot, besides its owner."""
    customer, shop = await _bot_shop(db, ctx, customer_id)
    try:
        await own_panel.add_admin(db, shop, body.telegram_id)
    except own_panel.OwnPanelError as exc:
        await db.rollback()
        raise HTTPException(status_code=_ADMIN_STATUS.get(exc.key, 422), detail=exc.key) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.admin_add",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id, "telegram_id": body.telegram_id},
    )
    await db.commit()
    return {"bot": await _bot(db, shop)}


@router.delete("/customers/{customer_id}/own-bot/admins/{telegram_id}")
async def remove_own_bot_admin(customer_id: int, telegram_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    customer, shop = await _bot_shop(db, ctx, customer_id)
    try:
        await own_panel.remove_admin(db, shop, telegram_id)
    except own_panel.OwnPanelError as exc:
        await db.rollback()
        raise HTTPException(status_code=_ADMIN_STATUS.get(exc.key, 422), detail=exc.key) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.admin_remove",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id, "telegram_id": telegram_id},
    )
    await db.commit()
    return {"bot": await _bot(db, shop)}


@router.get("/customers/{customer_id}/own-bot/webhook")
async def own_bot_webhook(customer_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.core import resale

    _customer_row, shop = await _bot_shop(db, ctx, customer_id)
    if shop.bot_token_enc is None:
        raise HTTPException(status_code=422, detail="own_bot_not_connected")
    return await resale.webhook_status(shop.id)


@router.post("/customers/{customer_id}/own-bot/webhook/repoint")
async def repoint_own_bot_webhook(customer_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.core import resale

    customer, shop = await _bot_shop(db, ctx, customer_id)
    if shop.bot_token_enc is None:
        raise HTTPException(status_code=422, detail="own_bot_not_connected")
    try:
        await resale.point_webhook(shop.id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="own_bot.webhook_repoint",
        entity="customer",
        entity_id=customer.id,
        after={"shop_id": shop.id},
    )
    await db.commit()
    return await resale.webhook_status(shop.id)


async def _child_bot(db: DB, ctx: StaffContext, shop_id: int) -> Shop:
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    shop = await db.get(Shop, shop_id)
    if (
        shop is None
        or shop.parent_id != ctx.shop_id
        or (shop.reseller_profile_id is None and shop.own_account_id is None)
    ):
        raise HTTPException(status_code=404, detail="not_found")
    return shop


@router.get("/customer-bots/{shop_id}/logs")
async def customer_bot_logs(
    shop_id: int,
    ctx: Manager,
    db: DB,
    category: Literal["all", "errors", "orders", "payments", "jobs", "audit"] = "all",
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=100, ge=10, le=300),
) -> dict[str, Any]:
    """Unified activity, order, payment, job and audit logs for a selected customer/reseller bot."""
    from paurubot.core import resale

    shop = await _child_bot(db, ctx, shop_id)
    return await resale.bot_logs(db, shop, category=category, q=q, limit=limit, staff=True)


@router.get("/customer-bots/{shop_id}/logs.csv")
async def customer_bot_logs_csv(
    shop_id: int,
    ctx: Manager,
    db: DB,
    category: Literal["all", "errors", "orders", "payments", "jobs", "audit"] = "all",
    q: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=300, ge=10, le=300),
) -> Response:
    """Export the selected customer/reseller bot logs as CSV."""
    from paurubot.core import resale

    shop = await _child_bot(db, ctx, shop_id)
    data = await resale.bot_logs(db, shop, category=category, q=q, limit=limit, staff=True)
    return Response(
        content=resale.bot_logs_csv(data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="bot-{shop.id}-logs.csv"'},
    )
