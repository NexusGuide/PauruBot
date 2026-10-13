"""Mini App API (APP-01). Every handler is scoped to the authenticated customer."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from paurubot.api.routes import webapp_staff
from paurubot.api.webapp_auth import WebApp
from paurubot.core import catalog, customers, marketing, orders, pricing, reseller_desk, tickets, trials
from paurubot.db.models import (
    Category,
    DiscountCode,
    Location,
    Order,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    Service,
    ServiceStatus,
    Ticket,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import PanelError
from paurubot.payments import engine as payments
from paurubot.payments.base import PaymentError
from paurubot.payments.chains import NETWORKS
from paurubot.provisioning import lifecycle
from paurubot.provisioning.subscription import customer_link
from paurubot.worker import queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webapp", tags=["webapp"])
MAX_RECEIPT = 8 * 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def service_view(service: Service, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    from paurubot.panels.registry import delivery_of
    from paurubot.provisioning.subscription import branded_url

    delivery = delivery_of(service.panel.kind if service.panel else None)
    view = _service_base(service, cfg)
    view["delivery"] = delivery
    if delivery == "account":
        view["subscription_url"] = None
        view["account"] = {
            "username": service.panel_username,
            "password": (service.panel_data or {}).get("password") or None,
        }
    elif delivery == "wireguard":
        branded = branded_url(service)
        view["subscription_url"] = None
        view["download_url"] = f"{branded}?download=1" if branded else None
    return view


def _service_base(service: Service, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": service.id,
        "label": service.label or service.panel_username,
        "username": service.panel_username,
        "status": service.status.value,
        "is_trial": service.is_trial,
        "data_limit": service.data_limit,
        "used_bytes": service.used_bytes,
        "expire_at": _iso(service.expire_at),
        "subscription_url": customer_link(service, cfg) if cfg is not None else service.subscription_url,
        "location": service.panel.location.title if service.panel and service.panel.location else None,
        "plan_id": service.plan_id,
        "panel_id": service.panel_id,
        "extras": _extras(service, cfg),
        "changes": sorted(c for c in ("renew", "upgrade", "revoke") if orders.can_change(service, c)),
    }


def _extras(service: Service, cfg: dict[str, Any] | None) -> dict[str, int | None] | None:
    if cfg is None:
        return None
    gb, days = orders.service_extras(cfg, service)
    return None if gb is None and days is None else {"gb_price": gb, "day_price": days}


async def _priced_view(ctx: Any, plan: Plan) -> dict[str, Any]:
    from paurubot.core import resale

    listed = await resale.list_price(ctx.session, plan, ctx.shop.id)
    view = plan_view(plan, (await pricing.plan_quote(ctx.session, ctx.customer, plan.id, listed)).amount)
    view["base_price"] = listed
    return view


def plan_view(plan: Plan, price: int) -> dict[str, Any]:
    return {
        "id": plan.id,
        "title": plan.title,
        "description": plan.description,
        "days": plan.days,
        "data_gb": plan.data_gb,
        "price": price,
        "base_price": plan.price,
        "category_id": plan.category_id,
        "location_id": plan.location_id or (plan.panel.location_id if plan.panel else None),
        "panel_id": plan.panel_id,
        "renewable": plan.renewable,
        "on_hold": plan.on_hold,
        "emoji": (plan.button_style or {}).get("emoji") or None,
        "badge": (plan.button_style or {}).get("badge") or None,
    }


def order_view(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "kind": order.kind.value,
        "status": order.status.value,
        "base_price": order.base_price,
        "reseller_discount": order.reseller_discount,
        "code_discount": order.code_discount,
        "amount": order.amount,
        "service_id": order.service_id,
        "plan_id": order.plan_id,
        "params": {
            k: v
            for k, v in (order.params or {}).items()
            if k in {"extra_gb", "extra_days", "credit", "label", "custom", "queued", "queued_until"}
        },
        "expires_at": _iso(order.expires_at),
    }


@router.get("/me")
async def me(ctx: WebApp) -> dict[str, Any]:
    customer, cfg = ctx.customer, ctx.cfg
    balance = await wallet_balance(ctx.session, ctx.shop.id, customer.id)
    trial_reason = await trials.eligibility(ctx.session, customer)
    profile = customer.reseller_profile
    return {
        "id": customer.id,
        "telegram_id": customer.telegram_id,
        "name": customer.first_name,
        "username": customer.username,
        "locale": customer.locale or ctx.shop.default_locale,
        "balance": balance,
        "currency": ctx.shop.currency,
        "shop": {
            "title": ctx.shop.title,
            "bot_username": ctx.shop.bot_username,
            "support": cfg["support_username"] or None,
            "languages": cfg["languages"],
        },
        "trial": {
            "available": trial_reason is None,
            "reason": trial_reason,
            "days": cfg["trial_days"],
            "gb": cfg["trial_gb"],
        },
        "referral": {
            "enabled": bool(cfg["referral_enabled"]),
            "percent": cfg["referral_percent"],
            "count": await customers.referral_count(ctx.session, customer.id),
        },
        "reseller": {
            "active": bool(profile and profile.is_current),
            "discount_percent": profile.discount_percent if profile else 0,
        },
        "extras": {
            "enabled": bool(cfg["extras_enabled"]),
            "gb_price": cfg["extra_gb_price"],
            "day_price": cfg["extra_day_price"],
            "upgrade": bool(cfg["upgrade_enabled"]),
        },
        "payment_methods": await payments.available(ctx.session, ctx.shop.id, customer),
        "topup_bounds": list(await payments.topup_bounds(ctx.session, customer)),
        "staff": await webapp_staff.describe(ctx),
        "preferences": marketing.view(customer),
    }


class MarketingBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    marketing_opt_out: StrictBool


@router.post("/me/marketing")
async def set_marketing(body: MarketingBody, ctx: WebApp) -> dict[str, Any]:
    return await marketing.set_preference(
        ctx.session, ctx.customer, opt_out=body.marketing_opt_out, source="miniapp"
    )


class LocaleBody(BaseModel):
    locale: str = Field(max_length=8)


@router.post("/me/locale")
async def set_locale(body: LocaleBody, ctx: WebApp) -> dict[str, Any]:
    if body.locale not in (ctx.cfg["languages"] or []):
        raise HTTPException(status_code=400, detail="unsupported_locale")
    ctx.customer.locale = body.locale
    return {"locale": body.locale}


@router.get("/catalog")
async def catalog_view(ctx: WebApp) -> dict[str, Any]:
    plans = await catalog.plans(ctx.session, ctx.shop.id, ctx.customer)
    loc_ids = {p.location_id or (p.panel.location_id if p.panel else None) for p in plans} - {None}
    cat_ids = {p.category_id for p in plans} - {None}
    locations = list(
        await ctx.session.scalars(
            select(Location)
            .where(Location.id.in_(loc_ids), Location.is_active.is_(True))
            .order_by(Location.sort)
        )
    )
    categories = list(
        await ctx.session.scalars(
            select(Category)
            .where(Category.id.in_(cat_ids), Category.is_active.is_(True))
            .order_by(Category.sort)
        )
    )
    return {
        "locations": [{"id": loc.id, "title": loc.title, "flag": loc.flag} for loc in locations],
        "categories": [{"id": c.id, "title": c.title} for c in categories],
        "plans": [await _priced_view(ctx, p) for p in plans],
        "custom": _custom_offer(ctx),
    }


def _custom_offer(ctx: WebApp) -> dict[str, Any] | None:
    cfg = ctx.cfg
    if not cfg["custom_buy_enabled"] or not (cfg["custom_panel_id"] or cfg["custom_location_id"]):
        return None
    percent = pricing.reseller_percent(ctx.customer)
    return {
        **orders.custom_limits(cfg),
        "gb_price": int(cfg["custom_gb_price"]),
        "day_price": int(cfg["custom_day_price"]),
        "base_price": int(cfg["custom_base_price"]),
        "discount_percent": percent,
    }


class OrderBody(BaseModel):
    kind: Literal["purchase", "renew", "extra_volume", "extra_days", "upgrade"]
    plan_id: int | None = None
    service_id: int | None = None
    extra_gb: int = Field(default=0, ge=0, le=10_000)
    extra_days: int = Field(default=0, ge=0, le=3650)
    code: str | None = Field(default=None, max_length=32)
    label: str | None = Field(default=None, max_length=48)
    custom_gb: int = Field(default=0, ge=0, le=100_000)
    custom_days: int = Field(default=0, ge=0, le=3650)


async def _owned_service(ctx: WebApp, service_id: int | None) -> Service | None:
    if not service_id:
        return None
    service = await ctx.session.get(Service, service_id)
    if service is None or service.customer_id != ctx.customer.id or service.status == ServiceStatus.DELETED:
        raise HTTPException(status_code=404, detail="service_not_found")
    return service


@router.post("/orders")
async def create_order(body: OrderBody, ctx: WebApp) -> dict[str, Any]:
    plan = None
    if body.plan_id:
        plan = await catalog.get_plan(ctx.session, ctx.shop.id, body.plan_id, ctx.customer)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan_unavailable")
    service = await _owned_service(ctx, body.service_id)
    code: DiscountCode | None = None
    if body.code:
        code = await pricing.find_code(ctx.session, ctx.shop.id, body.code)
        if code is None:
            raise HTTPException(status_code=400, detail="discount_invalid")
    else:
        code = await pricing.sticky_code(ctx.session, ctx.customer)
    try:
        draft = await orders.draft(
            ctx.session,
            ctx.customer,
            OrderKind(body.kind),
            plan=plan,
            service=service,
            extra_gb=body.extra_gb,
            extra_days=body.extra_days,
            label=body.label if ctx.customer.is_reseller else None,
            custom_gb=body.custom_gb if body.kind == "purchase" and not plan else 0,
            custom_days=body.custom_days if body.kind == "purchase" and not plan else 0,
        )
        try:
            order = await orders.create(ctx.session, ctx.customer, draft, code=code, source="webapp")
        except pricing.DiscountError:
            if body.code:
                raise
            order = await orders.create(ctx.session, ctx.customer, draft, source="webapp")
    except (orders.OrderError, pricing.DiscountError) as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    balance = await wallet_balance(ctx.session, ctx.shop.id, ctx.customer.id)
    return {"order": order_view(order), "balance": balance, "due": payments.amount_due(order, balance)}


async def _own_order(ctx: WebApp, order_id: int) -> Order:
    order = await ctx.session.get(Order, order_id)
    if order is None or order.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="order_not_found")
    return order


@router.get("/orders/{order_id}")
async def get_order(order_id: int, ctx: WebApp) -> dict[str, Any]:
    return {"order": order_view(await _own_order(ctx, order_id))}


class PayBody(BaseModel):
    method: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_]+$")


async def _payment_view(ctx: WebApp, started: payments.Started) -> dict[str, Any]:
    payment, checkout = started.payment, started.checkout
    view: dict[str, Any] = {"payment_id": payment.id, "provider": payment.provider, "amount": payment.amount}
    if payment.provider == "card":
        view["accounts"] = checkout.instructions.get("accounts", [])
    elif payment.provider == "stars":
        from aiogram.types import LabeledPrice

        from paurubot.notifications.telegram import get_bot

        view["invoice_link"] = await get_bot().create_invoice_link(
            title="Top-up",
            description=f"Payment #{payment.id}",
            payload=str(checkout.instructions["payload"]),
            currency="XTR",
            prices=[LabeledPrice(label="Top-up", amount=int(checkout.instructions["stars"]))],
        )
    elif payment.provider in NETWORKS:
        view["crypto"] = {
            key: checkout.instructions.get(key)
            for key in ("network", "label", "address", "amount", "memo", "expires_at")
        }
    elif checkout.redirect_url:
        view["redirect_url"] = checkout.redirect_url
        view["pay_amount"] = payments.amount_to_pay(payment)
    return view


@router.post("/orders/{order_id}/pay")
async def pay_order(order_id: int, body: PayBody, ctx: WebApp) -> dict[str, Any]:
    order = await _own_order(ctx, order_id)
    if order.status != OrderStatus.PENDING:
        raise HTTPException(status_code=409, detail="order_expired")
    if body.method == "wallet":
        try:
            await orders.settle_from_wallet(ctx.session, order.id)
        except orders.OrderError as exc:
            raise HTTPException(status_code=400, detail=exc.key) from exc
        return {"order": order_view(order)}
    try:
        started = await payments.pay_order(ctx.session, ctx.customer, order, body.method)
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    await ctx.session.commit()
    return {"order": order_view(order), "payment": await _payment_view(ctx, started)}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int, ctx: WebApp) -> dict[str, Any]:
    order = await _own_order(ctx, order_id)
    await orders.cancel(ctx.session, order.id, ctx.customer.id)
    return {"ok": True}


class TopupBody(BaseModel):
    amount: int = Field(gt=0, le=10**12)
    method: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_]+$")


@router.post("/wallet/topup")
async def topup(body: TopupBody, ctx: WebApp) -> dict[str, Any]:
    try:
        started = await payments.start(ctx.session, ctx.customer, body.method, body.amount)
    except PaymentError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    await ctx.session.commit()
    return {"payment": await _payment_view(ctx, started)}


class GiftBody(BaseModel):
    code: str = Field(min_length=1, max_length=32)


@router.post("/wallet/gift")
async def redeem_gift(body: GiftBody, ctx: WebApp) -> dict[str, Any]:
    from paurubot.core import gifts

    try:
        amount = await gifts.redeem(ctx.session, ctx.customer, body.code)
    except gifts.GiftError as exc:
        await ctx.session.rollback()
        raise HTTPException(status_code=400, detail=exc.key) from exc
    await ctx.session.commit()
    return {"amount": amount, "balance": await wallet_balance(ctx.session, ctx.shop.id, ctx.customer.id)}


@router.post("/payments/{payment_id}/receipt")
async def upload_receipt(payment_id: int, ctx: WebApp, file: UploadFile = File(...)) -> dict[str, Any]:
    payment = await ctx.session.get(Payment, payment_id)
    if payment is None or payment.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="payment_not_found")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=400, detail="receipt_expected")
    data = await file.read(MAX_RECEIPT + 1)
    if len(data) > MAX_RECEIPT:
        raise HTTPException(status_code=413, detail="receipt_too_large")
    file_id = await _store_receipt(ctx, data)
    try:
        duplicate = await payments.attach_receipt(
            ctx.session, payment, file_id=file_id, text=None, image=data
        )
    except PaymentError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    await queue.enqueue(
        ctx.session,
        "notify",
        "receipt_review",
        {"payment_id": payment.id, "duplicate_of": duplicate.id if duplicate else None},
        shop_id=ctx.shop.id,
        dedupe_key=f"payment:{payment.id}:review",
    )
    return {"status": PaymentStatus.VERIFYING.value}


async def _store_receipt(ctx: WebApp, data: bytes) -> str | None:
    """Upload the image to the customer's own chat so staff can see it as a Telegram photo."""
    from aiogram.types import BufferedInputFile

    from paurubot.notifications.telegram import get_bot

    try:
        message = await get_bot().send_photo(
            ctx.customer.telegram_id, BufferedInputFile(data, "receipt.jpg"), disable_notification=True
        )
    except Exception:  # the receipt still reaches review with its hashes, just without a preview
        log.warning("could not store receipt image for customer %s", ctx.customer.id)
        return None
    return message.photo[-1].file_id if message.photo else None


@router.get("/payments/{payment_id}")
async def payment_status(payment_id: int, ctx: WebApp) -> dict[str, Any]:
    payment = await ctx.session.get(Payment, payment_id)
    if payment is None or payment.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="payment_not_found")
    return {
        "id": payment.id,
        "status": payment.status.value,
        "amount": payment.amount,
        "fulfilled": payment.fulfilled_at is not None,
    }


@router.get("/wallet/history")
async def history(ctx: WebApp, before: int | None = None) -> dict[str, Any]:
    rows = await wallet.history(ctx.session, ctx.shop.id, ctx.customer.id, limit=20, before_id=before)
    return {
        "items": [
            {
                "id": e.id,
                "amount": e.amount,
                "balance_after": e.balance_after,
                "kind": j.kind.split(":")[-1],
                "created_at": _iso(e.created_at),
            }
            for e, j in rows
        ]
    }


@router.get("/services")
async def services(ctx: WebApp) -> dict[str, Any]:
    rows = await ctx.session.scalars(
        select(Service)
        .where(Service.customer_id == ctx.customer.id, Service.status != ServiceStatus.DELETED)
        .order_by(Service.id.desc())
    )
    return {"items": [service_view(s, ctx.cfg) for s in rows]}


@router.get("/services/{service_id}")
async def service_detail(service_id: int, ctx: WebApp, refresh: bool = False) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    if refresh:
        try:
            await lifecycle.refresh(ctx.session, service)
        except PanelError:
            pass
    return {"service": service_view(service, ctx.cfg)}


@router.get("/services/{service_id}/usage")
async def service_usage(service_id: int, ctx: WebApp, days: int = Query(default=7)) -> dict[str, Any]:
    from paurubot.core import usage_history

    if days not in (7, 30):
        raise HTTPException(status_code=422, detail="unsupported_usage_window")
    service = await _owned_service(ctx, service_id)
    assert service is not None
    # Explicit shop guard in addition to customer ownership; history never queries the panel.
    if service.shop_id != ctx.shop.id:
        raise HTTPException(status_code=404, detail="service_not_found")
    return await usage_history.series(ctx.session, service, days)


@router.get("/services/{service_id}/links")
async def service_links(service_id: int, ctx: WebApp) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    return {"links": await lifecycle.links(ctx.session, service)}


@router.post("/services/{service_id}/revoke")
async def service_revoke(service_id: int, ctx: WebApp) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    try:
        await lifecycle.revoke(ctx.session, service)
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    return {"service": service_view(service, ctx.cfg)}


class RenameBody(BaseModel):
    label: str = Field(min_length=1, max_length=48)


@router.post("/services/{service_id}/rename")
async def service_rename(service_id: int, body: RenameBody, ctx: WebApp) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    service.label = body.label.strip()
    return {"service": service_view(service, ctx.cfg)}


@router.post("/trial")
async def trial(ctx: WebApp) -> dict[str, Any]:
    try:
        order = await trials.request(ctx.session, ctx.customer)
    except trials.TrialError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    return {"order": order_view(order)}


# --- support tickets in the Mini App (F-14) -----------------------------------------------


def _ticket_view(ticket: Ticket) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "subject": ticket.subject,
        "status": ticket.status.value,
        "department_id": ticket.department_id,
        "created_at": _iso(ticket.created_at),
        "updated_at": _iso(ticket.updated_at),
    }


async def _own_ticket(ctx: WebApp, ticket_id: int) -> Ticket:
    ticket = await ctx.session.get(Ticket, ticket_id)
    if ticket is None or ticket.customer_id != ctx.customer.id:
        raise HTTPException(status_code=404, detail="ticket_not_found")
    return ticket


@router.get("/tickets")
async def my_tickets(ctx: WebApp) -> dict[str, Any]:
    rows = await tickets.for_customer(ctx.session, ctx.customer, limit=20)
    desks = await tickets.departments(ctx.session, ctx.shop.id)
    return {
        "items": [_ticket_view(row) for row in rows],
        "departments": [{"id": desk.id, "title": desk.title} for desk in desks],
    }


class TicketBody(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    department_id: int | None = None


@router.post("/tickets")
async def new_ticket(body: TicketBody, ctx: WebApp) -> dict[str, Any]:
    try:
        ticket = await tickets.open_ticket(
            ctx.session, ctx.customer, body.subject, source="webapp", department_id=body.department_id
        )
        await tickets.add_message(
            ctx.session, ticket, author_type="customer", author_id=ctx.customer.id, text=body.text
        )
    except tickets.TicketError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    await queue.enqueue(
        ctx.session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=ctx.shop.id
    )
    return {"ticket": _ticket_view(ticket)}


@router.get("/tickets/{ticket_id}")
async def ticket_thread(ticket_id: int, ctx: WebApp) -> dict[str, Any]:
    ticket = await _own_ticket(ctx, ticket_id)
    rows = await tickets.messages(ctx.session, ticket.id, limit=100)  # staff notes never come back
    return {
        "ticket": _ticket_view(ticket),
        "messages": [
            {
                "id": row.id,
                "from": "me" if row.author_type == "customer" else "support",
                "text": row.text,
                "has_attachment": bool(row.attachment_file_id),
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ],
    }


class TicketReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/reply")
async def ticket_answer(ticket_id: int, body: TicketReplyBody, ctx: WebApp) -> dict[str, Any]:
    ticket = await _own_ticket(ctx, ticket_id)
    try:
        await tickets.add_message(
            ctx.session, ticket, author_type="customer", author_id=ctx.customer.id, text=body.text
        )
    except tickets.TicketError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    await queue.enqueue(
        ctx.session, "notify", "ticket_new_message", {"ticket_id": ticket.id}, shop_id=ctx.shop.id
    )
    return {"ticket": _ticket_view(ticket)}


@router.post("/tickets/{ticket_id}/close")
async def ticket_close(ticket_id: int, ctx: WebApp) -> dict[str, Any]:
    ticket = await _own_ticket(ctx, ticket_id)
    await tickets.close(ctx.session, ticket)
    return {"ticket": _ticket_view(ticket)}


# --- the reseller's desk in the Mini App (TEN-15) ----------------------------------------


def _reseller(ctx: WebApp) -> None:
    profile = ctx.customer.reseller_profile
    if profile is None or not profile.is_current:
        raise HTTPException(status_code=403, detail="not_reseller")


@router.get("/reseller/desk")
async def reseller_desk_view(ctx: WebApp, days: int = Query(3, ge=1, le=60)) -> dict[str, Any]:
    _reseller(ctx)
    figures = await reseller_desk.figures(ctx.session, ctx.customer, days)
    return {
        "figures": {
            "active": figures.active,
            "expiring": figures.expiring,
            "month_orders": figures.month_orders,
            "month_amount": figures.month_amount,
        },
        "expiring": await reseller_desk.expiring_rows(ctx.session, ctx.customer, days),
    }


class RenewManyBody(BaseModel):
    service_ids: list[int] = Field(min_length=1, max_length=reseller_desk.RENEW_LIMIT)


@router.post("/reseller/renew")
async def reseller_renew(body: RenewManyBody, ctx: WebApp) -> dict[str, Any]:
    _reseller(ctx)
    services = await reseller_desk.owned_services(ctx.session, ctx.customer, body.service_ids)
    result = await reseller_desk.renew_many(ctx.session, ctx.customer, services)
    return {
        "renewed": [s.id for s in result.renewed],
        "failed": [{"id": s.id, "error": key} for s, key in result.failed],
        "balance": await wallet_balance(ctx.session, ctx.shop.id, ctx.customer.id),
    }
