"""The reseller's own management in its dedicated bot's Mini App (OPB-4).

A customer who was given a dedicated bot on its own panel (`core/own_panel.py`) runs it from that
bot's Mini App: its plans and prices, the card buyers pay to, the trial and the extras it offers,
and how its admin account is doing. Receipts are reviewed with the Mini App's usual staff cards.

Only the bot's merchant reaches these routes, and only in that bot: the shop always comes from
the bot that signed the Mini App session, never from the request, so one reseller can never touch
another shop.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.deps import StaffContext
from paurubot.api.routes.webapp_staff import staff_for
from paurubot.api.webapp_auth import WebApp, WebAppContext
from paurubot.audit.log import record
from paurubot.core import own_panel
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Customer,
    Order,
    OrderStatus,
    Panel,
    PanelAccount,
    Payment,
    Plan,
    Service,
    ServiceStatus,
    Shop,
    StaffRole,
)
from paurubot.payments import engine as payments

router = APIRouter(prefix="/api/webapp/own", tags=["webapp"])
MAX_PLANS = 50
EXPIRE_CHOICES = (15, 30, 60, 120, 240, 720, 1440)


async def _merchant(ctx: WebAppContext) -> StaffContext:
    staff = await staff_for(ctx) if own_panel.is_own(ctx.shop) else None
    if staff is None or staff.staff.role != StaffRole.MERCHANT:
        raise HTTPException(status_code=403, detail="forbidden")
    return staff


async def _panel(ctx: WebAppContext) -> Panel:
    panel = await ctx.session.scalar(
        select(Panel).where(Panel.shop_id == ctx.shop.id).order_by(Panel.id).limit(1)
    )
    if panel is None:
        raise HTTPException(status_code=409, detail="own_panel_unreachable")
    return panel


async def _groups(panel: Panel) -> list[dict[str, str]] | None:
    """The groups the admin account may give its users; None when the panel can't be read now."""
    from paurubot.panels import PanelError, driver_for

    try:
        options = await (await driver_for(panel)).list_options()
    except PanelError:
        return None
    return [{"id": option.id, "title": option.title} for option in options]


def _account(account: PanelAccount | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "username": account.username,
        "status": account.status,
        "data_limit": account.data_limit,
        "used_bytes": account.used_bytes,
        "users_count": account.users_count,
        "max_users": account.max_users,
        "expire_at": account.expire_at.isoformat() if account.expire_at else None,
    }


def _card(cfg: dict[str, Any]) -> dict[str, str] | None:
    cards = [c for c in cfg.get("card_accounts") or [] if isinstance(c, dict) and c.get("number")]
    if not cards:
        return None
    return {"number": str(cards[0]["number"]), "holder": str(cards[0].get("holder") or "")}


def _features(cfg: dict[str, Any], panel: Panel | None) -> dict[str, Any]:
    from paurubot.core.orders import extra_bounds

    options = (panel.options if panel is not None else None) or {}
    extras = bool(cfg["extras_enabled"])

    def bound(key: str) -> int:
        return int(cfg.get(key) or 0)

    return {
        "trial": {
            "enabled": bool(cfg["trial_enabled"]),
            "gb": int(cfg["trial_gb"]),
            "days": int(cfg["trial_days"]),
            "daily_quota": int(cfg["trial_daily_quota"]),
        },
        "renew": {
            "enabled": options.get("allow_renewals") is not False,
            "carry": str(cfg.get("renew_mode") or "") in {"extend_carry", "restart_carry"}
            or (not cfg.get("renew_mode") and not cfg.get("renew_resets_usage", True)),
        },
        "extra_gb": {
            "enabled": extras and options.get("sell_extra_volume") is not False,
            "price": int(cfg["extra_gb_price"]),
            "min": bound("extra_gb_min"),
            "max": bound("extra_gb_max"),
            "bounds": list(extra_bounds(cfg, "gb")),
        },
        "extra_days": {
            "enabled": extras and options.get("sell_extra_days") is not False,
            "price": int(cfg["extra_day_price"]),
            "min": bound("extra_day_min"),
            "max": bound("extra_day_max"),
            "bounds": list(extra_bounds(cfg, "day")),
        },
    }


def _plan(plan: Plan, sold: int = 0) -> dict[str, Any]:
    return {
        "id": plan.id,
        "title": plan.title,
        "data_gb": plan.data_gb,
        "days": plan.days,
        "price": int(plan.price),
        "group_ids": [str(g) for g in (plan.panel_options or {}).get("group_ids") or []],
        "is_active": plan.is_active,
        "renewable": plan.renewable,
        "sold": sold,
    }


@router.get("")
async def overview(ctx: WebApp) -> dict[str, Any]:
    await _merchant(ctx)
    db, shop = ctx.session, ctx.shop
    cfg = await shop_settings.load(db, shop.id, fresh=True)
    account = await db.get(PanelAccount, shop.own_account_id) if shop.own_account_id else None
    day = utcnow() - timedelta(days=1)
    sales_today = await db.scalar(
        select(func.count(Order.id)).where(
            Order.shop_id == shop.id, Order.status == OrderStatus.COMPLETED, Order.completed_at >= day
        )
    )
    customers = await db.scalar(select(func.count(Customer.id)).where(Customer.shop_id == shop.id))
    active = await db.scalar(
        select(func.count(Service.id)).where(
            Service.shop_id == shop.id, Service.status == ServiceStatus.ACTIVE
        )
    )
    pending = [
        p
        for p in await db.scalars(select(Payment).where(Payment.shop_id == shop.id, payments.review_filter()))
        if payments.awaiting_review(p)
    ]
    plans = list(await db.scalars(select(Plan.is_active).where(Plan.shop_id == shop.id)))
    parent = await db.get(Shop, shop.parent_id) if shop.parent_id else None
    panel = await db.scalar(select(Panel).where(Panel.shop_id == shop.id).order_by(Panel.id).limit(1))
    return {
        "title": shop.title,
        "bot_username": shop.bot_username,
        "main_bot": parent.bot_username if parent is not None else None,
        "account": _account(account),
        "paused": str(cfg.get(own_panel.PAUSED) or ""),
        "hold": bool(cfg.get(own_panel.HOLD)),
        "expires_at": cfg.get(own_panel.EXPIRES) or None,
        "figures": {
            "sales_today": int(sales_today or 0),
            "customers": int(customers or 0),
            "active_services": int(active or 0),
            "pending_receipts": len(pending),
        },
        "plans": {"active": sum(1 for on in plans if on), "total": len(plans)},
        "card": _card(cfg),
        "features": _features(cfg, panel),
    }


# --- plans ----------------------------------------------------------------------------------


@router.get("/plans")
async def plans(ctx: WebApp) -> dict[str, Any]:
    await _merchant(ctx)
    db = ctx.session
    rows = list(
        await db.scalars(select(Plan).where(Plan.shop_id == ctx.shop.id).order_by(Plan.sort, Plan.id))
    )
    sold = dict(
        (
            await db.execute(
                select(Service.plan_id, func.count(Service.id))
                .where(Service.shop_id == ctx.shop.id)
                .group_by(Service.plan_id)
            )
        ).all()
    )
    groups = await _groups(await _panel(ctx))
    return {
        "items": [_plan(p, int(sold.get(p.id, 0))) for p in rows],
        "groups": groups or [],
        "groups_ok": groups is not None,
    }


class PlanBody(BaseModel):
    title: str = Field(min_length=1, max_length=96)
    data_gb: int = Field(ge=0, le=100_000)  # 0: no data limit
    days: int = Field(ge=0, le=3650)  # 0: no end date
    price: int = Field(ge=0, le=10**11)
    group_ids: list[str] = Field(default_factory=list, max_length=50)
    is_active: bool = True
    renewable: bool = True


async def _apply(ctx: WebAppContext, plan: Plan, body: PlanBody) -> None:
    panel = await _panel(ctx)
    picked = sorted({g.strip() for g in body.group_ids if g.strip()})
    if picked:
        groups = await _groups(panel)
        if groups is None:
            raise HTTPException(status_code=422, detail="own_panel_unreachable")
        if not set(picked) <= {g["id"] for g in groups}:
            raise HTTPException(status_code=422, detail="own_group_not_allowed")
        if not all(g.isdigit() for g in picked):
            raise HTTPException(status_code=422, detail="own_group_not_allowed")
    plan.title = body.title.strip()[:96] or plan.title
    plan.data_gb, plan.days, plan.price = body.data_gb, body.days, body.price
    plan.is_active, plan.renewable = body.is_active, body.renewable
    plan.panel_id = panel.id
    plan.panel_options = {"group_ids": [int(g) for g in picked]} if picked else {}
    plan.delivery = "panel"


async def _audit(ctx: WebAppContext, staff: StaffContext, action: str, plan: Plan) -> None:
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action=action,
        entity="plan",
        entity_id=plan.id,
        after=_plan(plan),
    )


@router.post("/plans")
async def create_plan(body: PlanBody, ctx: WebApp) -> dict[str, Any]:
    staff = await _merchant(ctx)
    count = await ctx.session.scalar(select(func.count(Plan.id)).where(Plan.shop_id == ctx.shop.id))
    if int(count or 0) >= MAX_PLANS:
        raise HTTPException(status_code=422, detail="own_too_many_plans")
    plan = Plan(shop_id=ctx.shop.id, title=body.title.strip()[:96])
    await _apply(ctx, plan, body)
    plan.sort = int(count or 0)
    ctx.session.add(plan)
    await ctx.session.flush()
    await _audit(ctx, staff, "own.plan.create", plan)
    return {"plan": _plan(plan)}


async def _own_plan(ctx: WebAppContext, plan_id: int) -> Plan:
    plan = await ctx.session.get(Plan, plan_id)
    if plan is None or plan.shop_id != ctx.shop.id:
        raise HTTPException(status_code=404, detail="not_found")
    return plan


@router.put("/plans/{plan_id}")
async def update_plan(plan_id: int, body: PlanBody, ctx: WebApp) -> dict[str, Any]:
    staff = await _merchant(ctx)
    plan = await _own_plan(ctx, plan_id)
    await _apply(ctx, plan, body)
    await ctx.session.flush()
    await _audit(ctx, staff, "own.plan.update", plan)
    return {"plan": _plan(plan)}


@router.delete("/plans/{plan_id}")
async def delete_plan(plan_id: int, ctx: WebApp) -> dict[str, Any]:
    """A plan nobody bought goes; one with services is only taken off sale, so they keep it."""
    staff = await _merchant(ctx)
    plan = await _own_plan(ctx, plan_id)
    used = await ctx.session.scalar(select(func.count(Service.id)).where(Service.plan_id == plan.id))
    used = int(used or 0) + int(
        await ctx.session.scalar(select(func.count(Order.id)).where(Order.plan_id == plan.id)) or 0
    )
    await _audit(ctx, staff, "own.plan.delete" if not used else "own.plan.hide", plan)
    if used:
        plan.is_active = False
        return {"deleted": False, "plan": _plan(plan)}
    await ctx.session.delete(plan)
    return {"deleted": True}


# --- card and payment -----------------------------------------------------------------------


class PaymentBody(BaseModel):
    number: str = Field(min_length=1, max_length=40)
    holder: str = Field(min_length=2, max_length=64)
    order_expire_minutes: int = Field(default=60)


@router.put("/payment")
async def save_payment(body: PaymentBody, ctx: WebApp) -> dict[str, Any]:
    from paurubot.core import resale

    staff = await _merchant(ctx)
    try:
        card = resale.parse_card(f"{body.number} {body.holder}")
    except resale.ResaleError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from None
    if len(card["holder"]) < 2:
        raise HTTPException(status_code=422, detail="card_holder_required")
    if body.order_expire_minutes not in EXPIRE_CHOICES:
        raise HTTPException(status_code=422, detail="invalid_value")
    await shop_settings.set_many(
        ctx.session,
        ctx.shop.id,
        {
            "card_accounts": [card],
            "card_enabled": True,
            "card_auto_approve": False,  # the reseller sees and decides every receipt
            "order_expire_minutes": body.order_expire_minutes,
        },
    )
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.payment",
        entity="shop",
        entity_id=ctx.shop.id,
        after={"card": f"*{card['number'][-4:]}", "order_expire_minutes": body.order_expire_minutes},
    )
    return {"card": card, "order_expire_minutes": body.order_expire_minutes}


@router.get("/payment")
async def payment(ctx: WebApp) -> dict[str, Any]:
    await _merchant(ctx)
    cfg = await shop_settings.load(ctx.session, ctx.shop.id, fresh=True)
    return {"card": _card(cfg), "order_expire_minutes": int(cfg["order_expire_minutes"])}


# --- trial, renewals and extras ------------------------------------------------------------


class TrialBody(BaseModel):
    enabled: bool
    gb: int = Field(ge=1, le=100)
    days: int = Field(ge=1, le=30)
    daily_quota: int = Field(default=0, ge=0, le=10_000)  # 0: no daily cap


class RenewBody(BaseModel):
    enabled: bool
    carry: bool = False


class ExtraBody(BaseModel):
    enabled: bool
    price: int = Field(ge=0, le=10**9)
    min: int = Field(default=0, ge=0, le=10_000)
    max: int = Field(default=0, ge=0, le=10_000)


class FeaturesBody(BaseModel):
    trial: TrialBody
    renew: RenewBody
    extra_gb: ExtraBody
    extra_days: ExtraBody


def _check_range(extra: ExtraBody, cap: int) -> None:
    if extra.max > cap or extra.min > cap or (extra.max and extra.min > extra.max):
        raise HTTPException(status_code=422, detail="own_extra_range")


@router.put("/features")
async def save_features(body: FeaturesBody, ctx: WebApp) -> dict[str, Any]:
    from paurubot.core.orders import EXTRA_CAP

    staff = await _merchant(ctx)
    _check_range(body.extra_gb, EXTRA_CAP["gb"])
    _check_range(body.extra_days, EXTRA_CAP["day"])
    if (body.extra_gb.enabled and body.extra_gb.price <= 0) or (
        body.extra_days.enabled and body.extra_days.price <= 0
    ):
        raise HTTPException(status_code=422, detail="own_extra_price")
    values: dict[str, Any] = {
        "trial_enabled": body.trial.enabled,
        "trial_gb": body.trial.gb,
        "trial_days": body.trial.days,
        "trial_daily_quota": body.trial.daily_quota,
        "renew_mode": "extend_carry" if body.renew.carry else "extend_reset",
        "extras_enabled": body.extra_gb.enabled or body.extra_days.enabled,
        "extra_gb_price": body.extra_gb.price,
        "extra_gb_min": body.extra_gb.min,
        "extra_gb_max": body.extra_gb.max,
        "extra_day_price": body.extra_days.price,
        "extra_day_min": body.extra_days.min,
        "extra_day_max": body.extra_days.max,
    }
    await shop_settings.set_many(ctx.session, ctx.shop.id, values)
    panel = await _panel(ctx)
    panel.options = {
        **(panel.options or {}),
        "allow_renewals": body.renew.enabled,
        "sell_extra_volume": body.extra_gb.enabled,
        "sell_extra_days": body.extra_days.enabled,
    }
    await ctx.session.flush()
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.features",
        entity="shop",
        entity_id=ctx.shop.id,
        after={**values, "allow_renewals": body.renew.enabled},
    )
    cfg = await shop_settings.load(ctx.session, ctx.shop.id, fresh=True)
    return {"features": _features(cfg, panel)}


# ---------------------------------------------------------------- forced-join channels (GRW-07)

_JOIN_STATUS = {"channel_exists": 409, "bot_stopped": 503}


def _join_error(exc: Exception) -> HTTPException:
    from paurubot.core import forced_join

    code = exc.code if isinstance(exc, forced_join.ForcedJoinError) else "channel_not_found"
    return HTTPException(status_code=_JOIN_STATUS.get(code, 422), detail=code)


class ChannelBody(BaseModel):
    channel: str = Field(min_length=2, max_length=300)


class ChannelPatch(BaseModel):
    is_active: bool


@router.get("/channels")
async def own_channels(ctx: WebApp) -> dict[str, Any]:
    """The channels this bot's buyers must join; the bot must be an admin in each."""
    from paurubot.core import forced_join

    await _merchant(ctx)
    rows = await forced_join.listing(ctx.session, ctx.shop.id)
    return {"channels": [forced_join.view(r) for r in rows], "limit": forced_join.MAX_CHANNELS}


@router.post("/channels")
async def own_add_channel(body: ChannelBody, ctx: WebApp) -> dict[str, Any]:
    from paurubot.core import forced_join

    staff = await _merchant(ctx)
    try:
        row = await forced_join.add(ctx.session, ctx.shop.id, body.channel)
    except forced_join.ForcedJoinError as exc:
        raise _join_error(exc) from None
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.channel.add",
        entity="channel",
        entity_id=row.id,
        after={"chat_id": row.chat_id, "title": row.title},
    )
    return {"channel": forced_join.view(row)}


@router.put("/channels/{row_id}")
async def own_set_channel(row_id: int, body: ChannelPatch, ctx: WebApp) -> dict[str, Any]:
    from paurubot.core import forced_join

    staff = await _merchant(ctx)
    try:
        row = await forced_join.owned(ctx.session, ctx.shop.id, row_id)
    except forced_join.ForcedJoinError:
        raise HTTPException(status_code=404, detail="channel_not_found") from None
    row.is_active = body.is_active
    await ctx.session.flush()
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.channel.set",
        entity="channel",
        entity_id=row.id,
        after={"is_active": body.is_active},
    )
    return {"channel": forced_join.view(row)}


@router.delete("/channels/{row_id}")
async def own_remove_channel(row_id: int, ctx: WebApp) -> dict[str, bool]:
    from paurubot.core import forced_join

    staff = await _merchant(ctx)
    try:
        row = await forced_join.owned(ctx.session, ctx.shop.id, row_id)
    except forced_join.ForcedJoinError:
        raise HTTPException(status_code=404, detail="channel_not_found") from None
    await ctx.session.delete(row)
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.channel.remove",
        entity="channel",
        entity_id=row_id,
    )
    return {"deleted": True}


# --- who runs the bot -----------------------------------------------------------------------
# The owner (the buyer of the admin account) adds and removes the bot's admins here, without
# asking the main shop. An admin they add runs the bot like them but can't change this list.


class OwnAdminBody(BaseModel):
    telegram_id: int = Field(gt=0, le=10**13)


_ADMIN_STATUS = {"own_admin_exists": 409, "own_admin_not_found": 404}


async def _admins_view(ctx: WebAppContext, staff: StaffContext) -> dict[str, Any]:
    owner_tg = await own_panel.owner_telegram_id(ctx.session, ctx.shop)
    return {
        "admins": await own_panel.admins(ctx.session, ctx.shop),
        "max": own_panel.MAX_ADMINS,
        "can_edit": staff.staff.telegram_id == owner_tg,
    }


async def _owner(ctx: WebAppContext) -> StaffContext:
    staff = await _merchant(ctx)
    if staff.staff.telegram_id != await own_panel.owner_telegram_id(ctx.session, ctx.shop):
        raise HTTPException(status_code=403, detail="own_admin_owner_only")
    return staff


@router.get("/admins")
async def own_admins(ctx: WebApp) -> dict[str, Any]:
    return await _admins_view(ctx, await _merchant(ctx))


@router.post("/admins")
async def own_add_admin(body: OwnAdminBody, ctx: WebApp) -> dict[str, Any]:
    staff = await _owner(ctx)
    try:
        await own_panel.add_admin(ctx.session, ctx.shop, body.telegram_id)
    except own_panel.OwnPanelError as exc:
        raise HTTPException(status_code=_ADMIN_STATUS.get(exc.key, 422), detail=exc.key) from None
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.admin.add",
        entity="staff",
        entity_id=None,
        after={"telegram_id": body.telegram_id},
    )
    return await _admins_view(ctx, staff)


@router.delete("/admins/{telegram_id}")
async def own_remove_admin(telegram_id: int, ctx: WebApp) -> dict[str, Any]:
    staff = await _owner(ctx)
    try:
        await own_panel.remove_admin(ctx.session, ctx.shop, telegram_id)
    except own_panel.OwnPanelError as exc:
        raise HTTPException(status_code=_ADMIN_STATUS.get(exc.key, 422), detail=exc.key) from None
    await record(
        ctx.session,
        shop_id=ctx.shop.id,
        actor=staff.actor,
        action="own.admin.remove",
        entity="staff",
        entity_id=None,
        after={"telegram_id": telegram_id},
    )
    return await _admins_view(ctx, staff)
