"""Mini App API (APP-01). Every handler is scoped to the authenticated customer."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot.api.webapp_auth import WebApp
from paurubot.core import catalog, customers, orders, pricing, trials
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
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import PanelError
from paurubot.payments import engine as payments
from paurubot.payments.base import PaymentError
from paurubot.payments.chains import NETWORKS
from paurubot.provisioning import lifecycle
from paurubot.worker import queue

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webapp", tags=["webapp"])
MAX_RECEIPT = 8 * 1024 * 1024


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def service_view(service: Service) -> dict[str, Any]:
    return {
        "id": service.id,
        "label": service.label or service.panel_username,
        "username": service.panel_username,
        "status": service.status.value,
        "is_trial": service.is_trial,
        "data_limit": service.data_limit,
        "used_bytes": service.used_bytes,
        "expire_at": _iso(service.expire_at),
        "subscription_url": service.subscription_url,
        "location": service.panel.location.title if service.panel and service.panel.location else None,
        "plan_id": service.plan_id,
        "panel_id": service.panel_id,
    }


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
            if k in {"extra_gb", "extra_days", "credit", "label"}
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
            "active": bool(profile and profile.is_active),
            "discount_percent": profile.discount_percent if profile else 0,
        },
        "extras": {
            "enabled": bool(cfg["extras_enabled"]),
            "gb_price": cfg["extra_gb_price"],
            "day_price": cfg["extra_day_price"],
            "upgrade": bool(cfg["upgrade_enabled"]),
        },
        "payment_methods": await payments.available(ctx.session, ctx.shop.id),
        "topup_bounds": list(await payments.topup_bounds(ctx.session, customer)),
    }


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
        "plans": [plan_view(p, pricing.quote(p.price, ctx.customer).amount) for p in plans],
    }


class OrderBody(BaseModel):
    kind: Literal["purchase", "renew", "extra_volume", "extra_days", "upgrade"]
    plan_id: int | None = None
    service_id: int | None = None
    extra_gb: int = Field(default=0, ge=0, le=10_000)
    extra_days: int = Field(default=0, ge=0, le=3650)
    code: str | None = Field(default=None, max_length=32)
    label: str | None = Field(default=None, max_length=48)


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
    return {"items": [service_view(s) for s in rows]}


@router.get("/services/{service_id}")
async def service_detail(service_id: int, ctx: WebApp, refresh: bool = False) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    if refresh:
        try:
            await lifecycle.refresh(ctx.session, service)
        except PanelError:
            pass
    return {"service": service_view(service)}


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
    return {"service": service_view(service)}


class RenameBody(BaseModel):
    label: str = Field(min_length=1, max_length=48)


@router.post("/services/{service_id}/rename")
async def service_rename(service_id: int, body: RenameBody, ctx: WebApp) -> dict[str, Any]:
    service = await _owned_service(ctx, service_id)
    assert service is not None
    service.label = body.label.strip()
    return {"service": service_view(service)}


@router.post("/trial")
async def trial(ctx: WebApp) -> dict[str, Any]:
    try:
        order = await trials.request(ctx.session, ctx.customer)
    except trials.TrialError as exc:
        raise HTTPException(status_code=400, detail=exc.key) from exc
    return {"order": order_view(order)}
