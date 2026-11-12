"""Customers, wallets and services (staff view)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, Select, cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB

from paurubot.api.admin_common import (
    Page,
    apply_sort,
    owned,
    page_params,
    paginate,
    plain,
    scope_services,
    sort_params,
)
from paurubot.api.confirmations import ConfirmFields, challenge, guard
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.core import confirm, customers, sub_links
from paurubot.core import orders as orders_core
from paurubot.db.models import (
    Customer,
    LedgerAccount,
    LedgerAccountKind,
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Payment,
    Plan,
    Service,
    ServiceStatus,
)
from paurubot.db.search import ESCAPE, contains
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import PanelError
from paurubot.provisioning import lifecycle
from paurubot.provisioning.subscription import branded_url
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
    "color",
    "risk",
    "card_trust",
    "hidden_panel_ids",
    "created_at",
    "last_seen_at",
    "blocked_bot_at",
    "referrer_id",
    "is_test",
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
    "disabled_reason",
    "staff_note",
    "synced_at",
    "created_at",
]


CUSTOMER_SORT = {
    "id": Customer.id,
    "created_at": Customer.created_at,
    "last_seen_at": Customer.last_seen_at,
    "username": Customer.username,
}
SERVICE_SORT = {
    "id": Service.id,
    "expire_at": Service.expire_at,
    "used_bytes": Service.used_bytes,
    "data_limit": Service.data_limit,
    "created_at": Service.created_at,
    "status": Service.status,
}
BULK_LIMIT = 5000


def service_out(service: Service) -> dict[str, Any]:
    data = plain(service, SERVICE_FIELDS)
    data["panel"] = service.panel.name if service.panel else None
    data["customer"] = service.customer.staff_label if service.customer else None
    data["branded_url"] = branded_url(service)
    return data


@router.get("/customers")
async def list_customers(
    ctx: Annotated[StaffContext, Depends(require("customers.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    q: str = Query("", max_length=64),
    banned: bool | None = None,
    tag: str | None = Query(None, max_length=24),
    risk: Literal["none", "low", "medium", "high"] | None = None,
    color: str | None = Query(None, max_length=12),
    sort: str | None = Depends(sort_params),
    segment: Literal["active", "ending", "buyers", "none"] | None = None,
) -> dict[str, Any]:
    if ctx.scoped:
        return {"items": [], "total": 0}
    stmt = select(Customer).where(Customer.shop_id == ctx.shop_id)
    if segment:
        stmt = stmt.where(_segment(ctx.shop_id, segment))
    query = q.strip().lstrip("@")
    if query:
        conditions: list[ColumnElement[bool]] = [
            Customer.username.ilike(contains(query), escape=ESCAPE),
            Customer.first_name.ilike(contains(query), escape=ESCAPE),
            Customer.phone.ilike(contains(query), escape=ESCAPE),
        ]
        if query.isdigit():
            conditions += [Customer.telegram_id == int(query), Customer.id == int(query)]
        stmt = stmt.where(or_(*conditions))
    if banned is not None:
        stmt = stmt.where(Customer.is_banned.is_(banned))
    if tag:
        stmt = stmt.where(cast(Customer.tags, JSONB).contains([tag]))
    if risk:
        stmt = stmt.where(Customer.risk == risk)
    if color:
        stmt = stmt.where(Customer.color == color)
    stmt = apply_sort(stmt, sort, CUSTOMER_SORT, Customer.id)
    rows, total = await paginate(db, stmt, page)
    summary = await _summaries(db, ctx.shop_id, [c.id for c in rows])
    return {"items": [{**plain(c, CUSTOMER_FIELDS), **summary[c.id]} for c in rows], "total": total}


ENDING_DAYS = 3


def _segment(shop_id: int, segment: str) -> ColumnElement[bool]:
    """Quick picks on the customer list: by active services, services about to end, or purchases."""
    active = select(Service.id).where(
        Service.shop_id == shop_id, Service.customer_id == Customer.id, Service.status == ServiceStatus.ACTIVE
    )
    if segment == "active":
        return active.exists()
    if segment == "ending":
        soon = datetime.now(UTC) + timedelta(days=ENDING_DAYS)
        return active.where(Service.expire_at.is_not(None), Service.expire_at <= soon).exists()
    bought = (
        select(Order.id)
        .where(
            Order.shop_id == shop_id, Order.customer_id == Customer.id, Order.status == OrderStatus.COMPLETED
        )
        .exists()
    )
    return bought if segment == "buyers" else ~bought


async def _summaries(db: Any, shop_id: int, ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per row of the list: active services, the nearest end among them, and the wallet balance."""
    out: dict[int, dict[str, Any]] = {
        i: {"active_services": 0, "next_expiry": None, "balance": 0} for i in ids
    }
    if not ids:
        return out
    services = await db.execute(
        select(Service.customer_id, func.count(Service.id), func.min(Service.expire_at))
        .where(
            Service.shop_id == shop_id, Service.customer_id.in_(ids), Service.status == ServiceStatus.ACTIVE
        )
        .group_by(Service.customer_id)
    )
    for customer_id, count, nearest in services:
        out[customer_id]["active_services"] = int(count)
        out[customer_id]["next_expiry"] = nearest.isoformat() if nearest else None
    balances = await db.execute(
        select(LedgerAccount.customer_id, LedgerAccount.balance).where(
            LedgerAccount.shop_id == shop_id,
            LedgerAccount.kind == LedgerAccountKind.WALLET,
            LedgerAccount.customer_id.in_(ids),
        )
    )
    for customer_id, balance in balances:
        out[customer_id]["balance"] = int(balance or 0)
    return out


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
    color: Literal["red", "orange", "yellow", "green", "blue", "purple"] | None = None
    risk: Literal["none", "low", "medium", "high"] | None = None
    card_trust: Literal["normal", "trusted", "blocked"] | None = None
    hidden_panel_ids: list[int] | None = Field(default=None, max_length=200)
    is_test: bool | None = None


PATCHED = ["note", "tags", "locale", "color", "risk", "card_trust", "hidden_panel_ids", "is_test"]


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
    before = snapshot(customer, PATCHED)
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "hidden_panel_ids":
            for panel_id in set(value or []):
                await owned(db, Panel, panel_id, ctx)  # only this shop's panels (CAT-10)
            value = sorted(set(value or []))
        if key == "tags" and value is not None:
            value = [str(tag)[:24] for tag in value]
        if key == "risk" and value is None:
            value = "none"
        if key == "card_trust" and value is None:
            value = "normal"
        setattr(customer, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.update",
        entity="customer",
        entity_id=customer.id,
        before=before,
        after=snapshot(customer, PATCHED),
    )
    return {"customer": plain(customer, CUSTOMER_FIELDS)}


@router.post("/customers/{customer_id}/trial-again")
async def trial_again(
    customer_id: int, ctx: Annotated[StaffContext, Depends(require("customers.write"))], db: DB
) -> dict[str, Any]:
    """One more trial for this customer (CAT-11)."""
    from paurubot.core import trials

    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await owned(db, Customer, customer_id, ctx)
    if not await trials.allow_again(db, customer):
        raise HTTPException(status_code=409, detail="trial_not_used")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.trial_again",
        entity="customer",
        entity_id=customer.id,
    )
    return {"ok": True}


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
        await telegram.send_text(customer.telegram_id, esc(body.text), shop_id=customer.shop_id)
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
    q: str = Query("", max_length=512),  # long enough for a pasted subscription link (REF-07)
    status: ServiceStatus | None = None,
    panel_id: int | None = None,
    customer_id: int | None = None,
    plan_id: int | None = None,
    sort: str | None = Depends(sort_params),
) -> dict[str, Any]:
    filters = ServiceFilter(q=q, status=status, panel_id=panel_id, customer_id=customer_id, plan_id=plan_id)
    stmt = await _filtered_services(db, ctx, filters)
    stmt = apply_sort(stmt, sort, SERVICE_SORT, Service.id)
    rows, total = await paginate(db, stmt, page)
    return {"items": [service_out(s) for s in rows], "total": total}


class ServiceFilter(BaseModel):
    q: str = Field(default="", max_length=512)
    status: ServiceStatus | None = None
    panel_id: int | None = None
    customer_id: int | None = None
    plan_id: int | None = None


async def _filtered_services(db: DB, ctx: StaffContext, f: ServiceFilter) -> Select[Any]:
    stmt = select(Service).where(Service.shop_id == ctx.shop_id)
    stmt = await scope_services(db, stmt, ctx)
    if f.q.strip():
        text = f.q.strip()
        token = sub_links.link_token(text)
        if token is not None:
            stmt = stmt.where(sub_links.condition(token))  # a pasted subscription link (REF-07)
        else:
            stmt = stmt.where(
                or_(
                    Service.panel_username.ilike(contains(text), escape=ESCAPE),
                    Service.label.ilike(contains(text), escape=ESCAPE),
                )
            )
    if f.status is not None:
        stmt = stmt.where(Service.status == f.status)
    else:
        stmt = stmt.where(Service.status != ServiceStatus.DELETED)
    if f.panel_id:
        stmt = stmt.where(Service.panel_id == f.panel_id)
    if f.customer_id:
        stmt = stmt.where(Service.customer_id == f.customer_id)
    if f.plan_id:
        stmt = stmt.where(Service.plan_id == f.plan_id)
    return stmt


async def scoped_service(db: DB, service_id: int, ctx: StaffContext) -> Service:
    service = await owned(db, Service, service_id, ctx)
    from paurubot.api.admin_common import reseller_scope

    scope = await reseller_scope(db, ctx)
    if scope is not None and service.customer_id not in scope:
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


class ServiceAction(ConfirmFields):
    action: Literal["enable", "disable", "revoke", "reset_usage", "delete", "extend"]
    days: int = Field(default=0, ge=0, le=3650)
    gb: int = Field(default=0, ge=0, le=100_000)
    reason: str | None = Field(default=None, max_length=255)
    # Tell the customer, with the reason, when staff delete or disable a service (BOT-09).
    notify: bool = True


async def notify_staff_change(db: DB, service: Service, action: str, reason: str | None) -> None:
    alert = {"delete": "removed_by_staff", "disable": "disabled_by_staff"}.get(action)
    if alert is None:
        return
    await queue.enqueue(
        db,
        "notify",
        "service_alert",
        {"service_id": service.id, "alert": alert, "reason": (reason or "")[:255]},
        shop_id=service.shop_id,
    )


@router.post("/services/{service_id}/action")
async def service_action(
    service_id: int,
    body: ServiceAction,
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
) -> dict[str, Any]:
    service = await scoped_service(db, service_id, ctx)
    if body.action == "delete":
        await guard(db, ctx, "service.delete", service.id, service.panel_username, body)
    before = snapshot(service, ["status", "expire_at", "data_limit", "subscription_url"])
    try:
        # One owner per service: never write over a renewal or sync in flight (SEC-F09).
        async with lifecycle.exclusive(db, service):
            if body.action in {"enable", "disable"}:
                await lifecycle.set_enabled(db, service, body.action == "enable", "staff")
            elif body.action == "revoke":
                from paurubot.core.orders import can_change

                if not can_change(service, "revoke"):
                    raise HTTPException(status_code=422, detail="panel_cannot_revoke")
                await lifecycle.revoke(db, service)
            elif body.action == "reset_usage":
                if ctx.scoped:  # fresh data for nothing: the same as a free extension
                    raise HTTPException(status_code=403, detail="forbidden")
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
                from paurubot.core.orders import can_change

                if not can_change(service, "gift"):
                    raise HTTPException(status_code=422, detail="panel_cannot_extend")
                await lifecycle.extend(db, service, days=body.days, gb=body.gb)
    except lifecycle.ServiceBusy as exc:
        raise HTTPException(status_code=409, detail="service_busy") from exc
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
    if body.notify:
        await notify_staff_change(db, service, body.action, body.reason)
    return {"service": service_out(service)}


@router.post("/services/{service_id}/confirm-delete")
async def confirm_service_delete(
    service_id: int,
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
) -> dict[str, Any]:
    """Step one of deleting a service (F-22): type its username to confirm."""
    service = await scoped_service(db, service_id, ctx)
    if service.status == ServiceStatus.DELETED:
        raise HTTPException(status_code=409, detail="already_deleted")
    return await challenge(ctx, "service.delete", service.id, service.panel_username)


class NoteBody(BaseModel):
    note: str | None = Field(default=None, max_length=255)


@router.put("/services/{service_id}/note")
async def service_note(
    service_id: int,
    body: NoteBody,
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
) -> dict[str, Any]:
    """A note on a service only staff see (SVC-16)."""
    service = await scoped_service(db, service_id, ctx)
    before = service.staff_note
    service.staff_note = (body.note or "").strip() or None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.note",
        entity="service",
        entity_id=service.id,
        before={"staff_note": before},
        after={"staff_note": service.staff_note},
    )
    return {"service": service_out(service)}


class MoveBody(BaseModel):
    panel_id: int


async def _move_order(db: DB, ctx: StaffContext, service: Service, panel: Panel) -> int | None:
    """A free location change made by staff (SVC-04); None when the service can't go there."""
    from paurubot.core import catalog

    if service.status == ServiceStatus.DELETED or service.panel_id == panel.id:
        return None
    if await catalog.pick_panel_for(db, ctx.shop_id, panel.id, None) is None:
        return None
    order = Order(
        shop_id=ctx.shop_id,
        customer_id=service.customer_id,
        kind=OrderKind.MOVE,
        status=OrderStatus.PAID,
        service_id=service.id,
        params={"move_to": panel.id},
        base_price=0,
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
    return order.id


@router.post("/services/{service_id}/move")
async def move_service(
    service_id: int,
    body: MoveBody,
    ctx: Annotated[StaffContext, Depends(require("services.write"))],
    db: DB,
) -> dict[str, Any]:
    """Move a service to another panel with its remaining data and end date; the link stays (SVC-04)."""
    service = await scoped_service(db, service_id, ctx)
    panel = await owned(db, Panel, body.panel_id, ctx)
    order_id = await _move_order(db, ctx, service, panel)
    if order_id is None:
        raise HTTPException(status_code=409, detail="move_unavailable")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.move",
        entity="service",
        entity_id=service.id,
        before={"panel_id": service.panel_id},
        after={"panel_id": panel.id, "order_id": order_id},
    )
    return {"order_id": order_id}


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


async def _bulk_move(db: DB, ctx: StaffContext, ids: list[int], panel_id: int | None) -> dict[str, Any]:
    if panel_id is None:
        raise HTTPException(status_code=422, detail="panel_required")
    panel = await owned(db, Panel, panel_id, ctx)
    queued = 0
    for service_id in ids:
        service = await db.get(Service, service_id)
        if service is not None and await _move_order(db, ctx, service, panel) is not None:
            queued += 1
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.bulk_move",
        entity="service",
        after={"panel_id": panel.id, "count": queued},
    )
    return {"queued": queued, "skipped": len(ids) - queued}


class BulkServices(ConfirmFields):
    """Act on explicit ids or on everything matching a filter (SVC-07, ADM-04)."""

    action: Literal["enable", "disable", "extend", "delete", "move"]
    ids: list[int] = Field(default_factory=list, max_length=BULK_LIMIT)
    panel_id: int | None = None
    filter: ServiceFilter | None = None
    days: int = Field(default=0, ge=0, le=3650)
    gb: int = Field(default=0, ge=0, le=100_000)
    reason: str | None = Field(default=None, max_length=255)
    notify: bool = True


# Bulk actions that take services away from customers need the typed confirmation (F-22).
CONFIRMED_BULK = {"delete", "disable"}


async def _bulk_ids(db: DB, ctx: StaffContext, body: BulkServices) -> list[int]:
    if bool(body.ids) == (body.filter is not None):
        raise HTTPException(status_code=422, detail="ids_or_filter")
    if body.filter is not None:
        stmt = await _filtered_services(db, ctx, body.filter)
    else:
        stmt = await scope_services(
            db, select(Service).where(Service.shop_id == ctx.shop_id, Service.id.in_(body.ids)), ctx
        )
        stmt = stmt.where(Service.status != ServiceStatus.DELETED)
    ids = list(await db.scalars(stmt.with_only_columns(Service.id).limit(BULK_LIMIT + 1)))
    if len(ids) > BULK_LIMIT:
        raise HTTPException(status_code=422, detail="too_many")
    return ids


@router.post("/services/bulk/confirm")
async def confirm_bulk_services(
    body: BulkServices, ctx: Annotated[StaffContext, Depends(require("services.write"))], db: DB
) -> dict[str, Any]:
    """Step one of a bulk delete or disable (F-22): type how many services it touches."""
    if body.action not in CONFIRMED_BULK:
        raise HTTPException(status_code=422, detail="no_confirmation_needed")
    ids = await _bulk_ids(db, ctx, body)
    if not ids:
        raise HTTPException(status_code=422, detail="nothing_selected")
    target = confirm.selection_digest(body.action, ids)
    return {**await challenge(ctx, f"service.bulk_{body.action}", target, str(len(ids))), "count": len(ids)}


@router.post("/services/bulk")
async def bulk_services(
    body: BulkServices, ctx: Annotated[StaffContext, Depends(require("services.write"))], db: DB
) -> dict[str, Any]:
    """Queue one job per service so slow panels never block the request and failures retry."""
    if body.action == "extend":
        if ctx.scoped:
            raise HTTPException(status_code=403, detail="forbidden")
        if not body.days and not body.gb:
            raise HTTPException(status_code=422, detail="nothing_to_extend")
    ids = await _bulk_ids(db, ctx, body)
    if body.action in CONFIRMED_BULK:
        # The selection is part of the target: a filter that now matches more services needs a new one.
        target = confirm.selection_digest(body.action, ids)
        await guard(db, ctx, f"service.bulk_{body.action}", target, str(len(ids)), body)
    if body.action == "move":
        return await _bulk_move(db, ctx, ids, body.panel_id)
    batch = uuid.uuid4().hex[:12]
    payload = {
        "action": body.action,
        "days": body.days,
        "gb": body.gb,
        "reason": body.reason,
        "notify": body.notify and body.action in {"delete", "disable"},
    }
    for service_id in ids:
        await queue.enqueue(
            db,
            "bulk",
            "service",
            {**payload, "service_id": service_id, "staff_id": ctx.staff.id, "batch": batch},
            shop_id=ctx.shop_id,
            dedupe_key=f"bulk:{batch}:{service_id}",
            max_attempts=5,
        )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"service.bulk_{body.action}",
        entity="service",
        after={**payload, "count": len(ids), "batch": batch},
    )
    return {"queued": len(ids), "batch": batch}


class BulkCustomers(BaseModel):
    action: Literal["ban", "unban", "tag", "untag", "message"]
    ids: list[int] = Field(min_length=1, max_length=1000)
    reason: str | None = Field(default=None, max_length=255)
    tag: str | None = Field(default=None, min_length=1, max_length=24)
    text: str | None = Field(default=None, min_length=1, max_length=3500)
    # Foxima parity: a "reply" button under the message, which opens the customer's tickets.
    reply: bool = False


class NewCustomer(BaseModel):
    telegram_id: int = Field(ge=10_000, le=10**13)
    first_name: str | None = Field(default=None, max_length=128)
    username: str | None = Field(default=None, max_length=64)


@router.post("/customers")
async def create_customer(
    body: NewCustomer, ctx: Annotated[StaffContext, Depends(require("customers.write"))], db: DB
) -> dict[str, Any]:
    """Add a customer by Telegram id before they open the bot (e.g. to give them a service)."""
    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    if await customers.by_telegram(db, ctx.shop_id, body.telegram_id) is not None:
        raise HTTPException(status_code=409, detail="customer_exists")
    customer, _ = await customers.upsert_from_telegram(
        db,
        ctx.shop_id,
        body.telegram_id,
        username=(body.username or "").lstrip("@") or None,
        first_name=body.first_name,
    )
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.created",
        entity="customer",
        entity_id=customer.id,
    )
    await db.commit()
    return {"id": customer.id}


@router.delete("/customers/{customer_id}")
async def delete_customer(
    customer_id: int, ctx: Annotated[StaffContext, Depends(require("customers.ban"))], db: DB
) -> dict[str, Any]:
    """Remove a customer with no history at all; one with orders, payments, services or a wallet is
    banned instead, so the books stay right."""
    from paurubot.db.models import LedgerAccount, Order, Payment

    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    customer = await owned(db, Customer, customer_id, ctx)
    for model in (Order, Payment, Service, LedgerAccount):
        if await db.scalar(select(model.id).where(model.customer_id == customer.id).limit(1)) is not None:
            raise HTTPException(status_code=409, detail="customer_has_history")
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="customer.deleted",
        entity="customer",
        entity_id=customer.id,
        before={"telegram_id": customer.telegram_id, "name": customer.display_name},
    )
    await db.delete(customer)
    await db.commit()
    return {"ok": True}


@router.post("/customers/bulk")
async def bulk_customers(
    body: BulkCustomers, ctx: Annotated[StaffContext, Depends(require("customers.ban"))], db: DB
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    if body.action in {"tag", "untag", "message"} and not ctx.can("customers.write"):
        raise HTTPException(status_code=403, detail="forbidden")
    if body.action in {"tag", "untag"} and not body.tag:
        raise HTTPException(status_code=422, detail="tag_required")
    if body.action == "message":
        if not (body.text or "").strip():
            raise HTTPException(status_code=422, detail="text_required")
        ids = sorted(
            set(
                await db.scalars(
                    select(Customer.id).where(Customer.shop_id == ctx.shop_id, Customer.id.in_(body.ids))
                )
            )
        )
        await queue.enqueue(
            db,
            "notify",
            "staff_message",
            {"customer_ids": ids, "text": body.text, "reply": body.reply},
            shop_id=ctx.shop_id,
        )
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="customer.bulk_message",
            entity="customer",
            after={"ids": ids},
        )
        return {"queued": len(ids)}
    rows = list(
        await db.scalars(select(Customer).where(Customer.shop_id == ctx.shop_id, Customer.id.in_(body.ids)))
    )
    for customer in rows:
        if body.action in {"ban", "unban"}:
            await customers.set_ban(db, customer, body.action == "ban", body.reason, ctx.actor)
        else:
            tags = [t for t in (customer.tags or []) if t != body.tag]
            if body.action == "tag":
                tags.append(str(body.tag))
            customer.tags = tags[:20]
    if body.action in {"tag", "untag"}:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action=f"customer.bulk_{body.action}",
            entity="customer",
            after={"ids": [c.id for c in rows], "tag": body.tag},
        )
    return {"updated": len(rows)}


# --- trial users (ClockBot parity) -------------------------------------------------------


@router.get("/trial-users")
async def trial_users(
    ctx: Annotated[StaffContext, Depends(require("customers.read"))],
    db: DB,
    filter: Literal["all", "bought", "not_bought"] = "all",
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    """Customers who took a free trial, and whether they went on to buy (a completed order)."""
    from paurubot.db.models import Order, OrderKind, OrderStatus

    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    trial = (
        select(Service.customer_id, func.min(Service.created_at).label("trial_at"))
        .where(Service.shop_id == ctx.shop_id, Service.is_trial.is_(True))
        .group_by(Service.customer_id)
        .subquery()
    )
    bought = (
        select(Order.customer_id)
        .where(
            Order.shop_id == ctx.shop_id,
            Order.status == OrderStatus.COMPLETED,
            Order.kind.in_([OrderKind.PURCHASE, OrderKind.RENEW]),
            Order.amount > 0,
        )
        .distinct()
        .subquery()
    )
    is_buyer = trial.c.customer_id.in_(select(bought.c.customer_id))
    stmt = select(Customer, trial.c.trial_at, is_buyer.label("bought")).join(
        trial, trial.c.customer_id == Customer.id
    )
    if filter == "bought":
        stmt = stmt.where(is_buyer)
    elif filter == "not_bought":
        stmt = stmt.where(~is_buyer)
    rows = (await db.execute(stmt.order_by(trial.c.trial_at.desc()).limit(limit))).all()
    total = int(await db.scalar(select(func.count()).select_from(trial)) or 0)
    buyers = int(await db.scalar(select(func.count()).select_from(trial).where(is_buyer)) or 0)
    return {
        "summary": {"trials": total, "bought": buyers, "rate": round(buyers * 100 / total) if total else 0},
        "items": [
            {
                "customer": {
                    "id": c.id,
                    "name": c.staff_label,
                    "username": c.username,
                    "banned": c.is_banned,
                },
                "trial_at": trial_at.isoformat() if trial_at else None,
                "bought": bool(was_buyer),
            }
            for c, trial_at, was_buyer in rows
        ],
    }


# --- quick renew (ClockBot parity) -------------------------------------------------------


class QuickRenewBody(BaseModel):
    service_id: int | None = None


@router.post("/customers/{customer_id}/quick-renew")
async def quick_renew(
    customer_id: int,
    body: QuickRenewBody,
    ctx: Annotated[StaffContext, Depends(require("services.create"))],
    db: DB,
) -> dict[str, Any]:
    """Renew the customer's most urgent service (or the one named) with its own plan, free, as a
    staff gift: the same renewal the customer would buy, paid by the shop."""
    from paurubot.db.base import utcnow
    from paurubot.db.models import Order, OrderKind, OrderStatus, Plan

    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    customer = await owned(db, Customer, customer_id, ctx)
    live = [ServiceStatus.ACTIVE, ServiceStatus.ON_HOLD, ServiceStatus.LIMITED, ServiceStatus.EXPIRED]
    stmt = select(Service).where(
        Service.customer_id == customer.id,
        Service.status.in_(live),
        Service.is_trial.is_(False),
        Service.plan_id.is_not(None),
    )
    if body.service_id is not None:
        stmt = stmt.where(Service.id == body.service_id)
    services = list(await db.scalars(stmt))
    if not services:
        raise HTTPException(status_code=404, detail="no_service_to_renew")
    far = utcnow().replace(year=9999)
    service = min(services, key=lambda s: (s.expire_at or far, -s.id))
    pending = await db.scalar(
        select(Order.id).where(
            Order.service_id == service.id,
            Order.kind == OrderKind.RENEW,
            Order.status.in_([OrderStatus.PAID, OrderStatus.PROVISIONING]),
        )
    )
    if pending is not None:  # a second press must not give a second free renewal
        raise HTTPException(status_code=409, detail="renew_in_progress")
    plan = await db.get(Plan, service.plan_id)
    if plan is None or not plan.renewable:
        raise HTTPException(status_code=422, detail="plan_unavailable")
    draft = await orders_core.draft(db, customer, OrderKind.RENEW, plan=plan, service=service)
    order = Order(
        shop_id=customer.shop_id,
        customer_id=customer.id,
        kind=OrderKind.RENEW,
        status=OrderStatus.PAID,
        plan_id=plan.id,
        service_id=service.id,
        params={**draft.params, "staff_renew": f"staff:{ctx.staff.id}"},
        base_price=0,
        amount=0,
        paid_at=utcnow(),
        source="staff",
    )
    db.add(order)
    await db.flush()
    # The same provisioning as a bought renewal: it waits for the period to end when the shop
    # queues renewals (SVC-02), with the same retries.
    await orders_core.enqueue_provision(db, order)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="service.quick_renew",
        entity="service",
        entity_id=service.id,
        after={"order_id": order.id, "plan_id": plan.id},
    )
    await db.commit()
    return {"order_id": order.id, "service_id": service.id}


@router.get("/customers/{customer_id}/photo")
async def customer_photo(
    customer_id: int, ctx: Annotated[StaffContext, Depends(require("customers.read"))], db: DB
) -> Any:
    """The customer's Telegram profile picture, through the shop's bot (ClockBot parity)."""
    import io

    from fastapi.responses import Response

    from paurubot.notifications import telegram

    customer = await owned(db, Customer, customer_id, ctx)
    bot = await telegram.bot_for(customer.shop_id)
    if bot is None or customer.telegram_id <= 0:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        photos = await bot.get_user_profile_photos(customer.telegram_id, limit=1)
        if not photos.photos:
            raise HTTPException(status_code=404, detail="not_found")
        largest = photos.photos[0][-1]
        buffer = io.BytesIO()
        await bot.download(largest.file_id, destination=buffer)
    except HTTPException:
        raise
    except Exception as exc:  # the customer hid it, or Telegram is unreachable
        raise HTTPException(status_code=404, detail="not_found") from exc
    return Response(
        buffer.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
    )
