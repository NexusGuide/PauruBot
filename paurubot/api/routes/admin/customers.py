"""Customers, wallets and services (staff view)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, Select, cast, or_, select
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
    data["customer"] = service.customer.display_name if service.customer else None
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
    if tag:
        stmt = stmt.where(cast(Customer.tags, JSONB).contains([tag]))
    if risk:
        stmt = stmt.where(Customer.risk == risk)
    if color:
        stmt = stmt.where(Customer.color == color)
    stmt = apply_sort(stmt, sort, CUSTOMER_SORT, Customer.id)
    rows, total = await paginate(db, stmt, page)
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
    color: Literal["red", "orange", "yellow", "green", "blue", "purple"] | None = None
    risk: Literal["none", "low", "medium", "high"] | None = None
    card_trust: Literal["normal", "trusted", "blocked"] | None = None
    hidden_panel_ids: list[int] | None = Field(default=None, max_length=200)


PATCHED = ["note", "tags", "locale", "color", "risk", "card_trust", "hidden_panel_ids"]


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
                or_(Service.panel_username.ilike(f"%{text}%"), Service.label.ilike(f"%{text}%"))
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
    action: Literal["ban", "unban", "tag", "untag"]
    ids: list[int] = Field(min_length=1, max_length=1000)
    reason: str | None = Field(default=None, max_length=255)
    tag: str | None = Field(default=None, min_length=1, max_length=24)


@router.post("/customers/bulk")
async def bulk_customers(
    body: BulkCustomers, ctx: Annotated[StaffContext, Depends(require("customers.ban"))], db: DB
) -> dict[str, Any]:
    if ctx.scoped:
        raise HTTPException(status_code=404, detail="not_found")
    if body.action in {"tag", "untag"}:
        if not ctx.can("customers.write"):
            raise HTTPException(status_code=403, detail="forbidden")
        if not body.tag:
            raise HTTPException(status_code=422, detail="tag_required")
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
