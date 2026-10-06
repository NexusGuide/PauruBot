"""Customers, wallets and services (staff view)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, or_, select

from paurubot.api.admin_common import Page, owned, page_params, paginate, plain, scope_services
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.core import customers
from paurubot.db.models import (
    Customer,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import PanelError
from paurubot.provisioning import lifecycle
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])

CUSTOMER_FIELDS = [
    "id",
    "telegram_id",
    "username",
    "first_name",
    "phone",
    "locale",
    "is_banned",
    "ban_reason",
    "tags",
    "note",
    "created_at",
    "last_seen_at",
    "blocked_bot_at",
    "referrer_id",
]
SERVICE_FIELDS = [
    "id",
    "customer_id",
    "panel_id",
    "plan_id",
    "panel_username",
    "label",
    "status",
    "is_trial",
    "data_limit",
    "used_bytes",
    "expire_at",
    "subscription_url",
    "synced_at",
    "created_at",
]


def service_out(service: Service) -> dict[str, Any]:
    data = plain(service, SERVICE_FIELDS)
    data["panel"] = service.panel.name if service.panel else None
    data["customer"] = service.customer.display_name if service.customer else None
    return data


@router.get("/customers")
async def list_customers(
    ctx: Annotated[StaffContext, Depends(require("customers.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    q: str = Query("", max_length=64),
    banned: bool | None = None,
) -> dict[str, Any]:
    if ctx.scoped:
        return {"items": [], "total": 0}
    stmt = select(Customer).where(Customer.shop_id == ctx.shop_id)
    query = q.strip().lstrip("@")
    if query:
        conditions: list[ColumnElement[bool]] = [
            Customer.username.ilike(f"%{query}%"),
            Customer.first_name.ilike(f"%{query}%"),
            Customer.phone.ilike(f"%{query}%"),
        ]
        if query.isdigit():
            conditions += [Customer.telegram_id == int(query), Customer.id == int(query)]
        stmt = stmt.where(or_(*conditions))
    if banned is not None:
        stmt = stmt.where(Customer.is_banned.is_(banned))
    rows, total = await paginate(db, stmt.order_by(Customer.id.desc()), page)
    return {"items": [plain(c, CUSTOMER_FIELDS) for c in rows], "total": total}


@router.get("/customers/{customer_id}")
async def customer_detail(
    customer_id: int, ctx: Annotated[StaffContext, Depends(require("customers.read"))], db: DB
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await owned(db, Customer, customer_id, ctx)
    services = await db.scalars(
        select(Service).where(Service.customer_id == customer.id).order_by(Service.id.desc())
    )
    orders = await db.scalars(
        select(Order).where(Order.customer_id == customer.id).order_by(Order.id.desc()).limit(30)
    )
    payments = await db.scalars(
        select(Payment).where(Payment.customer_id == customer.id).order_by(Payment.id.desc()).limit(30)
    )
    history = await wallet.history(db, ctx.shop_id, customer.id, limit=30)
    profile = customer.reseller_profile
    return {
        "customer": plain(customer, CUSTOMER_FIELDS),
        "balance": await wallet_balance(db, ctx.shop_id, customer.id),
        "referrals": await customers.referral_count(db, customer.id),
        "reseller": None
        if profile is None
        else {"id": profile.id, "group_id": profile.group_id, "active": profile.is_active},
        "services": [service_out(s) for s in services],
        "orders": [
            plain(o, ["id", "kind", "status", "amount", "created_at", "completed_at", "error"])
            for o in orders
        ],
        "payments": [
            plain(p, ["id", "provider", "status", "amount", "created_at", "paid_at"]) for p in payments
        ],
        "ledger": [
            {
                "id": e.id,
                "amount": e.amount,
                "balance_after": e.balance_after,
                "kind": j.kind,
                "memo": j.memo,
                "at": e.created_at.isoformat(),
            }
            for e, j in history
        ],
    }


class CustomerPatch(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = Field(default=None, max_length=20)
    locale: str | None = Field(default=None, max_length=8)


@router.patch("/customers/{customer_id}")
async def update_customer(
    customer_id: int,
    body: CustomerPatch,
    ctx: Annotated[StaffContext, Depends(require("customers.write"))],
    db: DB,
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await owned(db, Customer, customer_id, ctx)
    before = snapshot(customer, ["note", "tags", "locale"])
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "tags" and value is not None:
            value = [str(tag)[:24] for tag in value]
        setattr(customer, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.update",
        entity="customer",
        entity_id=customer.id,
        before=before,
        after=snapshot(customer, ["note", "tags", "locale"]),
    )
    return {"customer": plain(customer, CUSTOMER_FIELDS)}


class BanBody(BaseModel):
    banned: bool
    reason: str | None = Field(default=None, max_length=255)


@router.post("/customers/{customer_id}/ban")
async def ban(
    customer_id: int, body: BanBody, ctx: Annotated[StaffContext, Depends(require("customers.ban"))], db: DB
) -> dict[str, Any]:
    customer = await owned(db, Customer, customer_id, ctx)
    await customers.set_ban(db, customer, body.banned, body.reason, ctx.actor)
    return {"customer": plain(customer, CUSTOMER_FIELDS)}


class AdjustBody(BaseModel):
    delta: int = Field(ge=-(10**11), le=10**11)
    memo: str = Field(min_length=2, max_length=200)
    request_id: str = Field(default_factory=lambda: uuid.uuid4().hex, max_length=64)
    notify: bool = True

    @field_validator("delta")
    @classmethod
    def _not_zero(cls, value: int) -> int:
        if value == 0:
            raise ValueError("delta must not be zero")
        return value


@router.post("/customers/{customer_id}/adjust")
async def adjust(
    customer_id: int,
    body: AdjustBody,
    ctx: Annotated[StaffContext, Depends(require("wallet.adjust"))],
    db: DB,
) -> dict[str, Any]:
    customer = await owned(db, Customer, customer_id, ctx)
    try:
        await wallet.adjust(
            db,
            ctx.shop_id,
            customer.id,
            body.delta,
            key=f"adjust:web:{ctx.shop_id}:{customer.id}:{body.request_id}",
            actor=f"staff:{ctx.staff.id}",
            memo=body.memo,
        )
    except wallet.InsufficientFunds as exc:
        raise HTTPException(status_code=400, detail="insufficient_balance") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="wallet.adjust",
        entity="customer",
        entity_id=customer.id,
        after={"delta": body.delta, "memo": body.memo},
    )
    if body.notify:
        await queue.enqueue(
            db,
            "notify",
            "wallet_adjusted",
            {"customer_id": customer.id, "delta": body.delta},
            shop_id=ctx.shop_id,
        )
    return {"balance": await wallet_balance(db, ctx.shop_id, customer.id)}


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/customers/{customer_id}/message")
async def message(
    customer_id: int,
    body: MessageBody,
    ctx: Annotated[StaffContext, Depends(require("customers.write"))],
    db: DB,
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await owned(db, Customer, customer_id, ctx)
    from paurubot.core.formatting import esc
    from paurubot.notifications import telegram

    try:
        await telegram.send_text(customer.telegram_id, esc(body.text))
    except telegram.Undeliverable as exc:
        raise HTTPException(status_code=409, detail="undeliverable") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.message",
        entity="customer",
        entity_id=customer.id,
    )
    return {"ok": True}


# --- services -------------------------------------------------------------------------


@router.get("/services")
async def list_services(
    ctx: Annotated[StaffContext, Depends(require("services.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    q: str = Query("", max_length=64),
    status: ServiceStatus | None = None,
    panel_id: int | None = None,
    customer_id: int | None = None,
) -> dict[str, Any]:
    stmt = select(Service).where(Service.shop_id == ctx.shop_id)
    stmt = await scope_services(db, stmt, ctx)
    if q.strip():
        stmt = stmt.where(
            or_(Service.panel_username.ilike(f"%{q.strip()}%"), Service.label.ilike(f"%{q.strip()}%"))
        )
    if status is not None:
        stmt = stmt.where(Service.status == status)
    else:
        stmt = stmt.where(Service.status != ServiceStatus.DELETED)
    if panel_id:
        stmt = stmt.where(Service.panel_id == panel_id)
    if customer_id:
        stmt = stmt.where(Service.customer_id == customer_id)
    rows, total = await paginate(db, stmt.order_by(Service.id.desc()), page)
    return {"items": [service_out(s) for s in rows], "total": total}


async def scoped_service(db: DB, service_id: int, ctx: StaffContext) -> Service:
    service = await owned(db, Service, service_id, ctx)
    from paurubot.api.admin_common import reseller_customer_id

    owner = await reseller_customer_id(db, ctx)
    if owner is not None and service.customer_id != owner:
        raise HTTPException(status_code=404, detail="not_found")
    return service


@router.get("/services/{service_id}")
async def service_detail(
    service_id: int,
    ctx: Annotated[StaffContext, Depends(require("services.read"))],
    db: DB,
    refresh: bool = False,
) -> dict[str, Any]:
    service = await scoped_service(db, service_id, ctx)
    if refresh:
        try:
            await lifecycle.refresh(db, service)
        except PanelError as exc:
            raise HTTPException(status_code=502, detail="panel_error") from exc
    return {"service": service_out(service)}


class ServiceAction(BaseModel):
    action: Literal["enable", "disable", "revoke", "reset_usage", "delete", "extend"]
    days: int = Field(default=0, ge=0, le=3650)
    gb: int = Field(default=0, ge=0, le=100_000)
    reason: str | None = Field(default=None, max_length=255)


@router.post("/services/{service_id}/action")
async def service_action(
    service_id: int,
    body: ServiceAction,
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
) -> dict[str, Any]:
    service = await scoped_service(db, service_id, ctx)
    before = snapshot(service, ["status", "expire_at", "data_limit", "subscription_url"])
    try:
        if body.action in {"enable", "disable"}:
            await lifecycle.set_enabled(db, service, body.action == "enable")
        elif body.action == "revoke":
            await lifecycle.revoke(db, service)
        elif body.action == "reset_usage":
            panel = await db.get(Panel, service.panel_id)
            assert panel is not None
            from paurubot.panels import driver_for

            await (await driver_for(panel)).reset_usage(lifecycle.ref(service))
            await lifecycle.refresh(db, service)
        elif body.action == "delete":
            await lifecycle.delete_service(db, service, body.reason or "deleted by staff")
        else:
            if ctx.scoped:
                raise HTTPException(status_code=403, detail="forbidden")
            await _extend(db, service, body.days, body.gb)
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"service.{body.action}",
        entity="service",
        entity_id=service.id,
        before=before,
        after=snapshot(service, ["status", "expire_at", "data_limit", "subscription_url"]),
    )
    return {"service": service_out(service)}


async def _extend(db: DB, service: Service, days: int, gb: int) -> None:
    from paurubot.panels import GB, UNSET, driver_for

    panel = await db.get(Panel, service.panel_id)
    assert panel is not None
    driver = await driver_for(panel)
    expire: Any = UNSET
    if days and service.expire_at:
        now = datetime.now(UTC).replace(microsecond=0)
        expire = (service.expire_at if service.expire_at > now else now) + timedelta(days=days)
    limit = int(service.data_limit) + gb * GB if gb and service.data_limit else None
    info = await driver.update_user(lifecycle.ref(service), expire_at=expire, data_limit=limit, enabled=True)
    lifecycle.apply_panel_user(service, info)


class ManualService(BaseModel):
    customer_id: int
    plan_id: int
    label: str | None = Field(default=None, max_length=48)


@router.post("/services")
async def create_service(
    body: ManualService, ctx: Annotated[StaffContext, Depends(require("services.create"))], db: DB
) -> dict[str, Any]:
    """Free service created by staff (CAT-06); goes through the normal provisioning queue."""
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    customer = await owned(db, Customer, body.customer_id, ctx)
    plan = await owned(db, Plan, body.plan_id, ctx)
    order = Order(
        shop_id=ctx.shop_id,
        customer_id=customer.id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.PAID,
        plan_id=plan.id,
        params={"label": body.label} if body.label else {},
        base_price=plan.price,
        amount=0,
        paid_at=datetime.now(UTC),
        source="staff",
    )
    db.add(order)
    await db.flush()
    await queue.enqueue(
        db,
        "provision",
        "order",
        {"order_id": order.id},
        shop_id=ctx.shop_id,
        dedupe_key=f"order:{order.id}:provision",
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.create_manual",
        entity="order",
        entity_id=order.id,
        after={"customer_id": customer.id, "plan_id": plan.id},
    )
    return {"order_id": order.id}
