"""Resellers (TEN-*), staff accounts and support tickets (SUP-01)."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from paurubot.api.admin_common import (
    Page,
    owned,
    page_params,
    paginate,
    plain,
    reseller_customer_id,
    reseller_profile,
)
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.auth import rbac, sessions
from paurubot.auth import staff as staff_service
from paurubot.core import tickets
from paurubot.core.resellers import estimate_runway, timeline, web_login
from paurubot.db.models import (
    ApplicationStatus,
    CannedReply,
    Customer,
    LedgerAccount,
    Panel,
    Plan,
    ResellerApplication,
    ResellerBillingSnapshot,
    ResellerGroup,
    ResellerPrice,
    ResellerProfile,
    Service,
    Staff,
    StaffApiKey,
    StaffRole,
    Ticket,
    TicketDepartment,
    TicketStatus,
)
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])
Manager = Annotated[StaffContext, Depends(require("resellers.manage"))]

GROUP_FIELDS = [
    "id",
    "title",
    "discount_percent",
    "min_topup",
    "max_active_services",
    "is_active",
    "hidden_panel_ids",
]
PROFILE_FIELDS = [
    "id",
    "customer_id",
    "group_id",
    "permissions",
    "is_active",
    "expires_at",
    "max_purchases_per_day",
    "parent_id",
    "discount_override",
    "max_subresellers",
    "billing_mode",
    "gb_price",
    "hourly_price",
    "credit_limit",
    "billing_debt",
    "last_billed_at",
    "low_balance_threshold",
    "max_active_services",
    "max_total_gb",
    "suspended_at",
    "suspend_reason",
    "created_at",
]
STAFF_FIELDS = [
    "id",
    "role",
    "username",
    "telegram_id",
    "display_name",
    "is_active",
    "reseller_profile_id",
    "last_login_at",
    "created_at",
]


class GroupBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    discount_percent: int = Field(ge=0, le=100)
    min_topup: int = Field(default=0, ge=0)
    max_active_services: int | None = Field(default=None, ge=1)
    is_active: bool = True
    hidden_panel_ids: list[int] = Field(default_factory=list, max_length=200)


async def _check_panels(db: DB, ctx: StaffContext, ids: list[int]) -> list[int]:
    for panel_id in set(ids):
        await owned(db, Panel, panel_id, ctx)
    return sorted(set(ids))


@router.get("/resellers")
async def resellers(ctx: Manager, db: DB) -> dict[str, Any]:
    groups = await db.scalars(
        select(ResellerGroup).where(ResellerGroup.shop_id == ctx.shop_id).order_by(ResellerGroup.id)
    )
    profiles = await db.execute(
        select(ResellerProfile, Customer)
        .join(Customer, Customer.id == ResellerProfile.customer_id)
        .where(ResellerProfile.shop_id == ctx.shop_id)
        .order_by(ResellerProfile.id.desc())
    )
    applications = await db.execute(
        select(ResellerApplication, Customer)
        .join(Customer, Customer.id == ResellerApplication.customer_id)
        .where(
            ResellerApplication.shop_id == ctx.shop_id,
            ResellerApplication.status == ApplicationStatus.PENDING,
        )
    )
    rows = profiles.all()
    balances = await _balances(db, ctx.shop_id, [c.id for _, c in rows])
    return {
        "groups": [plain(g, GROUP_FIELDS) for g in groups],
        "profiles": [
            {
                **plain(p, PROFILE_FIELDS),
                "customer": c.staff_label,
                "telegram_id": c.telegram_id,
                "balance": balances.get(c.id, 0),
                "discount_percent": p.discount_percent,
                "is_current": p.is_current,
            }
            for p, c in rows
        ],
        "applications": [
            {
                "id": a.id,
                "customer_id": c.id,
                "customer": c.staff_label,
                "text": a.text,
                "created_at": a.created_at.isoformat(),
            }
            for a, c in applications.all()
        ],
        "permissions": sorted(rbac.SCOPED_FOR_RESELLER),
    }


async def _balances(db: DB, shop_id: int, customer_ids: list[int]) -> dict[int, int]:
    if not customer_ids:
        return {}
    rows = await db.execute(
        select(LedgerAccount.customer_id, LedgerAccount.balance).where(
            LedgerAccount.shop_id == shop_id, LedgerAccount.customer_id.in_(customer_ids)
        )
    )
    return {int(cid): int(balance) for cid, balance in rows.all() if cid is not None}


@router.post("/reseller-groups")
async def create_group(body: GroupBody, ctx: Manager, db: DB) -> dict[str, Any]:
    body.hidden_panel_ids = await _check_panels(db, ctx, body.hidden_panel_ids)
    row = ResellerGroup(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller_group.create",
        entity="reseller_group",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, GROUP_FIELDS)


@router.put("/reseller-groups/{group_id}")
async def update_group(group_id: int, body: GroupBody, ctx: Manager, db: DB) -> dict[str, Any]:
    row = await owned(db, ResellerGroup, group_id, ctx)
    before = snapshot(row)
    body.hidden_panel_ids = await _check_panels(db, ctx, body.hidden_panel_ids)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller_group.update",
        entity="reseller_group",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, GROUP_FIELDS)


# --- fixed reseller prices per plan (TEN-12) ----------------------------------------------


class PriceBody(BaseModel):
    plan_id: int
    group_id: int | None = None
    profile_id: int | None = None
    price: int | None = Field(default=None, ge=1, le=10**11)  # None removes the fixed price


def _price_out(row: ResellerPrice) -> dict[str, Any]:
    return {
        "id": row.id,
        "plan_id": row.plan_id,
        "group_id": row.group_id,
        "profile_id": row.profile_id,
        "price": row.price,
    }


@router.get("/reseller-prices")
async def list_reseller_prices(
    ctx: Manager,
    db: DB,
    plan_id: int | None = None,
    group_id: int | None = None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    stmt = select(ResellerPrice).where(ResellerPrice.shop_id == ctx.shop_id)
    if plan_id is not None:
        stmt = stmt.where(ResellerPrice.plan_id == plan_id)
    if group_id is not None:
        stmt = stmt.where(ResellerPrice.group_id == group_id)
    if profile_id is not None:
        stmt = stmt.where(ResellerPrice.profile_id == profile_id)
    rows = await db.scalars(stmt.order_by(ResellerPrice.plan_id, ResellerPrice.id))
    return {"items": [_price_out(r) for r in rows]}


@router.put("/reseller-prices")
async def set_reseller_price(body: PriceBody, ctx: Manager, db: DB) -> dict[str, Any]:
    """Set or remove the fixed price of one plan for a reseller group or one reseller."""
    if (body.group_id is None) == (body.profile_id is None):
        raise HTTPException(status_code=422, detail="group_or_profile")
    await owned(db, Plan, body.plan_id, ctx)
    if body.group_id is not None:
        await owned(db, ResellerGroup, body.group_id, ctx)
        owner = ResellerPrice.group_id == body.group_id
    else:
        await owned(db, ResellerProfile, int(body.profile_id or 0), ctx)
        owner = ResellerPrice.profile_id == body.profile_id
    row = await db.scalar(select(ResellerPrice).where(ResellerPrice.plan_id == body.plan_id, owner))
    before = _price_out(row) if row else None
    if body.price is None:
        if row is not None:
            await db.delete(row)
        row = None
    elif row is None:
        row = ResellerPrice(
            shop_id=ctx.shop_id,
            plan_id=body.plan_id,
            group_id=body.group_id,
            profile_id=body.profile_id,
            price=body.price,
        )
        db.add(row)
    else:
        row.price = body.price
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller_price.set",
        entity="plan",
        entity_id=body.plan_id,
        before=before,
        after=_price_out(row) if row else None,
    )
    return {"item": _price_out(row) if row else None}


class ProfileBody(BaseModel):
    customer_id: int
    group_id: int | None = None
    permissions: list[str] = Field(default_factory=list)
    is_active: bool = True
    expires_at: datetime | None = None
    max_purchases_per_day: int | None = Field(default=None, ge=1)
    parent_id: int | None = None
    discount_override: int | None = Field(default=None, ge=0, le=100)
    max_subresellers: int | None = Field(default=None, ge=0, le=10_000)
    billing_mode: Literal["prepaid", "usage", "hourly"] = "prepaid"
    gb_price: int = Field(default=0, ge=0, le=10**9)
    hourly_price: int = Field(default=0, ge=0, le=10**9)
    credit_limit: int = Field(default=0, ge=0, le=10**11)
    low_balance_threshold: int | None = Field(default=None, ge=0, le=10**11)
    max_active_services: int | None = Field(default=None, ge=1)
    max_total_gb: int | None = Field(default=None, ge=1)


@router.put("/resellers")
async def upsert_profile(body: ProfileBody, ctx: Manager, db: DB) -> dict[str, Any]:
    customer = await owned(db, Customer, body.customer_id, ctx)
    if body.group_id is not None:
        await owned(db, ResellerGroup, body.group_id, ctx)
    unknown = set(body.permissions) - rbac.SCOPED_FOR_RESELLER
    if unknown:
        raise HTTPException(status_code=422, detail="permission_not_allowed_for_resellers")
    profile = await db.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
    await _check_parent(db, ctx, body, profile)
    if body.billing_mode == "usage" and body.gb_price <= 0:
        raise HTTPException(status_code=422, detail="gb_price_required")
    if body.billing_mode == "hourly" and body.hourly_price <= 0:
        raise HTTPException(status_code=422, detail="hourly_price_required")
    before = snapshot(profile) if profile else None
    was_metered = profile is not None and profile.billing_mode in {"usage", "hourly"}
    if profile is None:
        profile = ResellerProfile(shop_id=ctx.shop_id, customer_id=customer.id)
        db.add(profile)
    switching_to_metered = body.billing_mode != "prepaid" and not was_metered
    for key, value in body.model_dump(exclude={"customer_id"}).items():
        setattr(profile, key, value)
    if switching_to_metered:
        await _start_metering(db, profile)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.upsert",
        entity="reseller_profile",
        entity_id=profile.id,
        before=before,
        after=snapshot(profile),
    )
    return plain(profile, PROFILE_FIELDS)


async def _check_parent(
    db: DB, ctx: StaffContext, body: ProfileBody, profile: ResellerProfile | None
) -> None:
    if body.parent_id is None:
        return
    parent = await owned(db, ResellerProfile, body.parent_id, ctx)
    if parent.parent_id is not None or (profile is not None and parent.id == profile.id):
        raise HTTPException(status_code=422, detail="parent_must_be_level_one")
    if profile is not None:
        has_children = await db.scalar(
            select(ResellerProfile.id).where(ResellerProfile.parent_id == profile.id).limit(1)
        )
        if has_children:
            raise HTTPException(status_code=422, detail="reseller_has_subresellers")
    if body.discount_override is not None and body.discount_override > parent.discount_percent:
        raise HTTPException(status_code=422, detail="subreseller_discount_too_high")


async def _start_metering(db: DB, profile: ResellerProfile) -> None:
    """Usage before the switch is already paid for: start counting from now."""
    from paurubot.db.base import utcnow

    profile.last_billed_at = utcnow()
    services = await db.scalars(select(Service).where(Service.customer_id == profile.customer_id))
    for service in services:
        service.billed_bytes = int(service.used_bytes or 0)


class SuspendBody(BaseModel):
    suspended: bool


@router.post("/resellers/{profile_id}/suspend")
async def suspend_reseller(profile_id: int, body: SuspendBody, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.core import resellers

    profile = await owned(db, ResellerProfile, profile_id, ctx)
    if body.suspended and profile.suspended_at is None:
        changed = await resellers.suspend(db, profile, "staff")
    elif not body.suspended and profile.suspended_at is not None:
        changed = await resellers.resume(db, profile)
    else:
        changed = 0
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.suspend" if body.suspended else "reseller.resume",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"services": changed},
    )
    return {"ok": True, "services": changed}


SNAPSHOT_FIELDS = [
    "id",
    "period_start",
    "period_end",
    "used_bytes",
    "service_hours",
    "active_services",
    "amount",
    "charged",
    "debt_after",
]


async def _billing_view(db: DB, profile: ResellerProfile) -> dict[str, Any]:
    rows = await db.scalars(
        select(ResellerBillingSnapshot)
        .where(ResellerBillingSnapshot.profile_id == profile.id)
        .order_by(ResellerBillingSnapshot.id.desc())
        .limit(100)
    )
    return {
        "billing_mode": profile.billing_mode,
        "debt": profile.billing_debt,
        "credit_limit": profile.credit_limit,
        "suspended_at": profile.suspended_at.isoformat() if profile.suspended_at else None,
        "items": [plain(r, SNAPSHOT_FIELDS) for r in rows],
    }


async def _billing_csv(db: DB, profile: ResellerProfile) -> Response:
    rows = list(
        await db.scalars(
            select(ResellerBillingSnapshot)
            .where(ResellerBillingSnapshot.profile_id == profile.id)
            .order_by(ResellerBillingSnapshot.id.desc())
            .limit(500)
        )
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "period_start",
            "period_end",
            "used_bytes",
            "service_hours",
            "active_services",
            "amount",
            "charged",
            "debt_after",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.period_start.isoformat(),
                row.period_end.isoformat(),
                int(row.used_bytes or 0),
                int(row.service_hours or 0),
                int(row.active_services or 0),
                int(row.amount or 0),
                int(row.charged or 0),
                int(row.debt_after or 0),
            ]
        )
    body = ("﻿" + buf.getvalue()).encode("utf-8")
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="reseller-{profile.id}-billing.csv"'},
    )


@router.get("/resellers/{profile_id}/billing")
async def reseller_billing(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    return await _billing_view(db, await owned(db, ResellerProfile, profile_id, ctx))


@router.get("/resellers/{profile_id}/billing.csv")
async def reseller_billing_csv(profile_id: int, ctx: Manager, db: DB) -> Response:
    return await _billing_csv(db, await owned(db, ResellerProfile, profile_id, ctx))


@router.post("/resellers/{profile_id}/billing/bill-now")
async def reseller_bill_now(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.core import resellers

    profile = await owned(db, ResellerProfile, profile_id, ctx)
    if not profile.is_metered:
        raise HTTPException(status_code=422, detail="reseller_not_metered")
    res = await resellers.bill_now(db, profile)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.bill_now",
        entity="reseller_profile",
        entity_id=profile.id,
        after={
            "amount": res.snapshot.amount if res.snapshot else 0,
            "charged": res.snapshot.charged if res.snapshot else 0,
            "debt": profile.billing_debt,
            "suspended": res.suspended,
            "resumed": res.resumed,
        },
    )
    view = await _billing_view(db, profile)
    view.update(
        billed=res.snapshot is not None,
        amount=res.snapshot.amount if res.snapshot else 0,
        charged=res.snapshot.charged if res.snapshot else 0,
        suspended=res.suspended,
        resumed=res.resumed,
    )
    return view


@router.post("/resellers/{profile_id}/billing/settle")
async def reseller_settle_debt(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    from paurubot.core import resellers

    profile = await owned(db, ResellerProfile, profile_id, ctx)
    res = await resellers.settle_debt(db, profile)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.debt_settled",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"charged": res.charged, "debt_after": res.debt_after, "resumed": res.resumed},
    )
    view = await _billing_view(db, profile)
    view.update(charged=res.charged, resumed=res.resumed)
    return view


# What the reseller itself sees of an audit row; staff also get who did it and the before/after.
OWN_AUDIT_FIELDS = ("at", "type", "kind")


async def _hub_view(db: DB, profile: ResellerProfile, *, staff: bool) -> dict[str, Any]:
    """The reseller hub (F-21): wallet runway plus the account's timeline, newest first."""
    runway = await estimate_runway(db, profile)
    items = []
    for item in await timeline(db, profile, limit=100):
        if item["type"] == "audit" and not staff:
            item = {key: item[key] for key in OWN_AUDIT_FIELDS}
        items.append({**item, "at": item["at"].isoformat() if item["at"] else None})
    return {"runway": runway.as_dict(), "timeline": items}


@router.get("/resellers/{profile_id}/hub")
async def reseller_hub(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    return await _hub_view(db, await owned(db, ResellerProfile, profile_id, ctx), staff=True)


# --- level-2 resellers, managed by a level-1 reseller (TEN-01) ---------------------------

SubManager = Annotated[StaffContext, Depends(require("resellers.sub"))]
SUB_FIELDS = ["id", "customer_id", "discount_override", "is_active", "expires_at", "created_at"]


async def _own_profile(db: DB, ctx: StaffContext) -> ResellerProfile:
    profile = await reseller_profile(db, ctx)
    if profile is None or profile.parent_id is not None:
        raise HTTPException(status_code=404, detail="not_found")
    return profile


@router.get("/my/billing")
async def my_billing(
    ctx: Annotated[StaffContext, Depends(require("dashboard.view"))], db: DB
) -> dict[str, Any]:
    profile = await reseller_profile(db, ctx)
    if profile is None:
        raise HTTPException(status_code=404, detail="not_found")
    return await _billing_view(db, profile)


@router.get("/my/billing.csv")
async def my_billing_csv(
    ctx: Annotated[StaffContext, Depends(require("dashboard.view"))], db: DB
) -> Response:
    profile = await _own_profile(db, ctx)
    return await _billing_csv(db, profile)


@router.post("/my/billing/settle")
async def my_settle_debt(
    ctx: Annotated[StaffContext, Depends(require("dashboard.view"))], db: DB
) -> dict[str, Any]:
    from paurubot.core import resellers

    profile = await _own_profile(db, ctx)
    res = await resellers.settle_debt(db, profile)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.debt_settled",
        entity="reseller_profile",
        entity_id=profile.id,
        after={"charged": res.charged, "debt_after": res.debt_after, "resumed": res.resumed},
    )
    view = await _billing_view(db, profile)
    view.update(charged=res.charged, resumed=res.resumed)
    return view


@router.get("/my/hub")
async def my_hub(ctx: Annotated[StaffContext, Depends(require("dashboard.view"))], db: DB) -> dict[str, Any]:
    profile = await reseller_profile(db, ctx)
    if profile is None:
        raise HTTPException(status_code=404, detail="not_found")
    return await _hub_view(db, profile, staff=False)


@router.get("/sub-resellers")
async def sub_resellers(ctx: SubManager, db: DB) -> dict[str, Any]:
    from paurubot.core import resellers

    parent = await _own_profile(db, ctx)
    rows = await resellers.children(db, parent)
    names = {
        c.id: c
        for c in await db.scalars(select(Customer).where(Customer.id.in_([r.customer_id for r in rows])))
    }
    balances = await _balances(db, ctx.shop_id, [r.customer_id for r in rows])
    stats = await resellers.sub_reseller_stats(db, parent, rows)
    return {
        "max_discount": parent.discount_percent,
        "limit": parent.max_subresellers,
        "total_commission_month": sum(s.commission_month for s in stats.values()),
        "items": [
            {
                **plain(r, SUB_FIELDS),
                "customer": names[r.customer_id].staff_label if r.customer_id in names else None,
                "telegram_id": names[r.customer_id].telegram_id if r.customer_id in names else None,
                "discount_percent": r.discount_percent,
                "balance": balances.get(r.customer_id, 0),
                "orders_month": stats[r.id].orders_month,
                "sales_month": stats[r.id].sales_month,
                "commission_month": stats[r.id].commission_month,
            }
            for r in rows
        ],
    }


class SubCreateBody(BaseModel):
    telegram_id: int = Field(gt=0)
    discount_percent: int = Field(ge=0, le=100)


class SubUpdateBody(BaseModel):
    discount_percent: int = Field(ge=0, le=100)
    is_active: bool = True


@router.post("/sub-resellers")
async def add_sub_reseller(body: SubCreateBody, ctx: SubManager, db: DB) -> dict[str, Any]:
    from paurubot.core import resellers

    parent = await _own_profile(db, ctx)
    customer = await db.scalar(
        select(Customer).where(Customer.shop_id == ctx.shop_id, Customer.telegram_id == body.telegram_id)
    )
    if customer is None:
        raise HTTPException(status_code=404, detail="customer_must_start_bot")
    try:
        profile = await resellers.make_child(db, parent, customer, body.discount_percent)
    except resellers.ResellerError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="subreseller.create",
        entity="reseller_profile",
        entity_id=profile.id,
        after=snapshot(profile),
    )
    return plain(profile, SUB_FIELDS)


@router.put("/sub-resellers/{profile_id}")
async def update_sub_reseller(
    profile_id: int, body: SubUpdateBody, ctx: SubManager, db: DB
) -> dict[str, Any]:
    from paurubot.core import resellers

    parent = await _own_profile(db, ctx)
    profile = await owned(db, ResellerProfile, profile_id, ctx)
    if profile.parent_id != parent.id:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        resellers.check_child_discount(parent, body.discount_percent)
    except resellers.ResellerError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from exc
    before = snapshot(profile)
    profile.discount_override = body.discount_percent
    profile.is_active = body.is_active
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="subreseller.update",
        entity="reseller_profile",
        entity_id=profile.id,
        before=before,
        after=snapshot(profile),
    )
    return plain(profile, SUB_FIELDS)


class DecisionBody(BaseModel):
    approve: bool
    group_id: int | None = None


@router.post("/reseller-applications/{application_id}")
async def decide(application_id: int, body: DecisionBody, ctx: Manager, db: DB) -> dict[str, Any]:
    application = await owned(db, ResellerApplication, application_id, ctx)
    if application.status != ApplicationStatus.PENDING:
        raise HTTPException(status_code=409, detail="already_decided")
    login = None
    if body.approve:
        if body.group_id is None:
            raise HTTPException(status_code=422, detail="group_required")
        await owned(db, ResellerGroup, body.group_id, ctx)
        profile = await db.scalar(
            select(ResellerProfile).where(ResellerProfile.customer_id == application.customer_id)
        )
        if profile is None:
            profile = ResellerProfile(
                shop_id=ctx.shop_id,
                customer_id=application.customer_id,
                group_id=body.group_id,
                permissions=sorted(rbac.DEFAULT_FOR_RESELLER),
            )
            db.add(profile)
            await db.flush()
        else:
            profile.group_id, profile.is_active = body.group_id, True
        login = await web_login(db, profile)
    application.status = ApplicationStatus.APPROVED if body.approve else ApplicationStatus.REJECTED
    application.reviewed_by_staff_id = ctx.staff.id
    application.reviewed_at = datetime.now(UTC)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="reseller.approve" if body.approve else "reseller.reject",
        entity="reseller_application",
        entity_id=application.id,
    )
    await queue.enqueue(
        db,
        "notify",
        "reseller_decision",
        {
            "customer_id": application.customer_id,
            "approved": body.approve,
            "web_username": login.username if body.approve and login is not None else None,
        },
        shop_id=ctx.shop_id,
    )
    return {"ok": True}


# --- staff ---------------------------------------------------------------------------


class StaffBody(BaseModel):
    role: StaffRole
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str | None = Field(default=None, min_length=10, max_length=256)
    telegram_id: int | None = Field(default=None, gt=0)
    display_name: str | None = Field(default=None, max_length=96)
    reseller_profile_id: int | None = None
    is_active: bool = True


@router.get("/staff")
async def list_staff(
    ctx: Annotated[StaffContext, Depends(require("staff.manage"))], db: DB
) -> dict[str, Any]:
    rows = await db.scalars(select(Staff).where(Staff.shop_id == ctx.shop_id).order_by(Staff.id))
    return {
        "items": [plain(s, STAFF_FIELDS) for s in rows],
        "roles": [r.value for r in StaffRole],
        "matrix": {r.value: rbac.permissions_for(r) for r in StaffRole},
    }


@router.post("/staff")
async def create_staff(
    body: StaffBody, ctx: Annotated[StaffContext, Depends(require("staff.manage"))], db: DB
) -> dict[str, Any]:
    if body.role in {StaffRole.OWNER, StaffRole.MERCHANT}:
        raise HTTPException(status_code=422, detail="cannot_create_owner")
    if body.role == StaffRole.RESELLER:
        if body.reseller_profile_id is None:
            raise HTTPException(status_code=422, detail="reseller_profile_required")
        await owned(db, ResellerProfile, body.reseller_profile_id, ctx)
    if not body.password and not body.telegram_id:
        raise HTTPException(status_code=422, detail="password_or_telegram_required")
    try:
        row = await staff_service.create_staff(
            db,
            ctx.shop_id,
            role=body.role,
            username=body.username,
            password=body.password,
            telegram_id=body.telegram_id,
            display_name=body.display_name,
            reseller_profile_id=body.reseller_profile_id,
        )
    except staff_service.AuthError as exc:
        raise HTTPException(status_code=409, detail="username_taken") from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="staff.create",
        entity="staff",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, STAFF_FIELDS)


class StaffPatch(BaseModel):
    role: StaffRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)
    telegram_id: int | None = Field(default=None, gt=0)
    display_name: str | None = Field(default=None, max_length=96)


@router.patch("/staff/{staff_id}")
async def update_staff(
    staff_id: int, body: StaffPatch, ctx: Annotated[StaffContext, Depends(require("staff.manage"))], db: DB
) -> dict[str, Any]:
    row = await owned(db, Staff, staff_id, ctx)
    if row.id == ctx.staff.id and (body.role is not None or body.is_active is False):
        raise HTTPException(status_code=422, detail="cannot_change_own_role")
    if row.role == StaffRole.OWNER or body.role in {StaffRole.OWNER, StaffRole.MERCHANT}:
        raise HTTPException(status_code=422, detail="owner_is_fixed")
    if body.role == StaffRole.RESELLER and row.reseller_profile_id is None:
        raise HTTPException(status_code=422, detail="reseller_profile_required")
    before = snapshot(row)
    access_changed = False
    if body.role is not None and body.role != row.role:
        row.role = body.role
        row.session_version += 1
        access_changed = True
    if body.is_active is not None and body.is_active != row.is_active:
        row.is_active = body.is_active
        row.session_version += 1
        access_changed = True
    if body.telegram_id is not None:
        row.telegram_id = body.telegram_id
    if body.display_name is not None:
        row.display_name = body.display_name
    if body.password:
        await staff_service.change_password(db, row, body.password)
    elif access_changed:
        await db.execute(
            update(StaffApiKey)
            .where(StaffApiKey.staff_id == row.id, StaffApiKey.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await sessions.destroy_all(row.id)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="staff.update",
        entity="staff",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, STAFF_FIELDS)


# --- tickets -------------------------------------------------------------------------


@router.get("/tickets")
async def list_tickets(
    ctx: Annotated[StaffContext, Depends(require("tickets.read"))],
    db: DB,
    page: Annotated[Page, Depends(page_params)],
    status: TicketStatus | None = None,
    department_id: int | None = None,
    priority: Literal["low", "normal", "high", "urgent"] | None = None,
    assigned: Literal["me", "none"] | None = None,
    assigned_staff_id: int | None = None,
    awaiting: bool | None = None,
) -> dict[str, Any]:
    stmt = select(Ticket).where(Ticket.shop_id == ctx.shop_id)
    owner = await reseller_customer_id(db, ctx)
    if owner is not None:
        stmt = stmt.where(Ticket.customer_id == owner)
    if status:
        stmt = stmt.where(Ticket.status == status)
    # The help desk filters (F-14).
    if department_id is not None:
        stmt = stmt.where(
            Ticket.department_id.is_(None) if department_id == 0 else Ticket.department_id == department_id
        )
    if priority:
        stmt = stmt.where(Ticket.priority == priority)
    if assigned == "me":
        stmt = stmt.where(Ticket.assigned_staff_id == ctx.staff.id)
    elif assigned == "none":
        stmt = stmt.where(Ticket.assigned_staff_id.is_(None))
    elif assigned_staff_id is not None:
        stmt = stmt.where(Ticket.assigned_staff_id == assigned_staff_id)
    if awaiting is not None:
        waiting = (Ticket.status != TicketStatus.CLOSED) & Ticket.last_customer_at.is_not(None)
        waiting = waiting & (
            Ticket.last_staff_at.is_(None) | (Ticket.last_staff_at < Ticket.last_customer_at)
        )
        stmt = stmt.where(waiting if awaiting else ~waiting)
    rows, total = await paginate(db, stmt.order_by(Ticket.updated_at.desc()), page)
    names = {
        c.id: c.staff_label
        for c in await db.scalars(select(Customer).where(Customer.id.in_([t.customer_id for t in rows])))
    }
    return {
        "items": [{**_ticket_row(t), "customer": names.get(t.customer_id)} for t in rows],
        "total": total,
    }


TICKET_FIELDS = [
    "id",
    "subject",
    "status",
    "source",
    "customer_id",
    "department_id",
    "priority",
    "is_complaint",
    "assigned_staff_id",
    "last_customer_at",
    "last_staff_at",
    "created_at",
    "updated_at",
]


MESSAGE_FIELDS = [
    "id",
    "author_type",
    "author_id",
    "text",
    "attachment_kind",
    "is_internal",
    "created_at",
    "reaction",  # the customer's rating of a staff reply (F-22)
]


def _ticket_row(ticket: Ticket) -> dict[str, Any]:
    return plain(ticket, TICKET_FIELDS) | {
        "awaiting": tickets.awaiting_staff(ticket),
        "service_context": ticket.service_context,
        "service_id": ticket.service_id,
    }


async def _ticket(db: DB, ticket_id: int, ctx: StaffContext) -> Ticket:
    ticket = await owned(db, Ticket, ticket_id, ctx)
    owner = await reseller_customer_id(db, ctx)
    if owner is not None and ticket.customer_id != owner:
        raise HTTPException(status_code=404, detail="not_found")
    return ticket


@router.get("/tickets/stats")
async def ticket_stats(
    ctx: Annotated[StaffContext, Depends(require("tickets.read"))], db: DB
) -> dict[str, Any]:
    """Help-desk KPIs: counts by status, awaiting reply, average first response time, CSAT."""
    owner = await reseller_customer_id(db, ctx)
    base = [Ticket.shop_id == ctx.shop_id]
    if owner is not None:
        base.append(Ticket.customer_id == owner)

    status_rows = (
        await db.execute(select(Ticket.status, func.count(Ticket.id)).where(*base).group_by(Ticket.status))
    ).all()
    by_status = {st.value if hasattr(st, "value") else str(st): int(cnt) for st, cnt in status_rows}
    awaiting = int(
        await db.scalar(
            select(func.count(Ticket.id)).where(
                *base,
                Ticket.status != TicketStatus.CLOSED,
                Ticket.last_customer_at.is_not(None),
                (Ticket.last_staff_at.is_(None)) | (Ticket.last_customer_at > Ticket.last_staff_at),
            )
        )
        or 0
    )

    first_staff = (
        select(
            TicketMessage.ticket_id.label("tid"),
            func.min(TicketMessage.created_at).label("first_reply"),
        )
        .where(TicketMessage.author_type == "staff", TicketMessage.is_internal.is_(False))
        .group_by(TicketMessage.ticket_id)
        .subquery()
    )
    reply_pairs = (
        await db.execute(
            select(Ticket.created_at, first_staff.c.first_reply)
            .join(first_staff, first_staff.c.tid == Ticket.id)
            .where(*base)
            .order_by(Ticket.id.desc())
            .limit(500)
        )
    ).all()
    deltas = [
        max(0.0, (replied - opened).total_seconds() / 60.0)
        for opened, replied in reply_pairs
        if opened is not None and replied is not None
    ]
    avg_minutes = round(sum(deltas) / len(deltas), 1) if deltas else None

    react_rows = (
        await db.execute(
            select(TicketMessage.reaction, func.count(TicketMessage.id))
            .join(Ticket, Ticket.id == TicketMessage.ticket_id)
            .where(*base, TicketMessage.reaction.in_(["up", "down"]))
            .group_by(TicketMessage.reaction)
        )
    ).all()
    reacts = {str(r): int(cnt) for r, cnt in react_rows}
    up, down = reacts.get("up", 0), reacts.get("down", 0)
    total_reacts = up + down
    satisfaction = round(up * 100.0 / total_reacts, 1) if total_reacts else None
    return {
        "open": by_status.get("open", 0),
        "answered": by_status.get("answered", 0),
        "closed": by_status.get("closed", 0),
        "awaiting": awaiting,
        "avg_first_reply_minutes": avg_minutes,
        "reactions_up": up,
        "reactions_down": down,
        "satisfaction_percent": satisfaction,
    }


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int, ctx: Annotated[StaffContext, Depends(require("tickets.read"))], db: DB
) -> dict[str, Any]:
    ticket = await _ticket(db, ticket_id, ctx)
    customer = await db.get(Customer, ticket.customer_id)
    # A reseller reads its own ticket as the customer side, so staff notes stay hidden from it.
    staff_view = await reseller_customer_id(db, ctx) is None
    messages = await tickets.messages(db, ticket.id, limit=200, include_internal=staff_view)
    return {
        "ticket": _ticket_row(ticket),
        "customer": customer.staff_label if customer else None,
        "messages": [
            plain(m, MESSAGE_FIELDS)
            | {
                "has_attachment": bool(m.attachment_file_id or m.attachment_size),
                "attachment_expired": m.attachment_expired,
            }
            for m in messages
        ],
    }


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/reply")
async def reply(
    ticket_id: int, body: ReplyBody, ctx: Annotated[StaffContext, Depends(require("tickets.reply"))], db: DB
) -> dict[str, Any]:
    ticket = await _ticket(db, ticket_id, ctx)
    own = await reseller_customer_id(db, ctx)
    try:
        if own is not None:
            # A reseller writes on its own ticket as the customer side; the shop's staff answer it.
            message = await tickets.add_message(
                db, ticket, author_type="customer", author_id=own, text=body.text
            )
            await queue.enqueue(
                db,
                "notify",
                "ticket_new_message",
                {"ticket_id": ticket.id, "message_id": message.id},
                shop_id=ctx.shop_id,
            )
        else:
            await tickets.add_message(db, ticket, author_type="staff", author_id=ctx.staff.id, text=body.text)
            await queue.enqueue(db, "notify", "ticket_reply", {"ticket_id": ticket.id}, shop_id=ctx.shop_id)
    except tickets.TicketError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    return {"ok": True}


class NewTicketBody(BaseModel):
    subject: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    department_id: int | None = None


@router.post("/tickets")
async def open_internal_ticket(
    body: NewTicketBody, ctx: Annotated[StaffContext, Depends(require("tickets.reply"))], db: DB
) -> dict[str, Any]:
    """A reseller writes to the shop owner from the web panel (SUP-02)."""
    own = await reseller_customer_id(db, ctx)
    if own is None:
        raise HTTPException(status_code=404, detail="not_found")
    customer = await db.get(Customer, own)
    assert customer is not None
    try:
        ticket = await tickets.open_ticket(
            db, customer, body.subject, source="panel", department_id=body.department_id
        )
        message = await tickets.add_message(db, ticket, author_type="customer", author_id=own, text=body.text)
    except tickets.TicketError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    await queue.enqueue(
        db,
        "notify",
        "ticket_new_message",
        {"ticket_id": ticket.id, "message_id": message.id},
        shop_id=ctx.shop_id,
    )
    return {"id": ticket.id}


@router.post("/tickets/{ticket_id}/close")
async def close(
    ticket_id: int, ctx: Annotated[StaffContext, Depends(require("tickets.reply"))], db: DB
) -> dict[str, Any]:
    ticket = await _ticket(db, ticket_id, ctx)
    was_closed = ticket.status == TicketStatus.CLOSED
    await tickets.close(db, ticket)
    if not was_closed and await reseller_customer_id(db, ctx) is None:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="ticket.close",
            entity="ticket",
            entity_id=ticket.id,
        )
        await queue.enqueue(
            db,
            "notify",
            "ticket_closed",
            {"customer_id": ticket.customer_id, "ticket_id": ticket.id, "subject": ticket.subject},
            shop_id=ctx.shop_id,
            dedupe_key=f"ticket:{ticket.id}:closed:{int(ticket.updated_at.timestamp())}",
        )
    return {"ok": True}


@router.post("/tickets/{ticket_id}/reopen")
async def reopen_ticket(
    ticket_id: int, ctx: Annotated[StaffContext, Depends(require("tickets.reply"))], db: DB
) -> dict[str, Any]:
    ticket = await _staff_ticket(db, ticket_id, ctx)
    changed = await tickets.reopen(db, ticket)
    if changed:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="ticket.reopen",
            entity="ticket",
            entity_id=ticket.id,
        )
    return {"ok": True, "ticket": _ticket_row(ticket)}


@router.post("/tickets/{ticket_id}/attachments")
async def upload_ticket_attachment(
    ticket_id: int,
    request: Request,
    ctx: Annotated[StaffContext, Depends(require("tickets.reply"))],
    db: DB,
) -> dict[str, Any]:
    """Staff uploads an image or text file directly into a ticket thread and delivers it to the user."""
    from paurubot.core import ticket_media

    ticket = await _staff_ticket(db, ticket_id, ctx)
    if ticket.status == TicketStatus.CLOSED:
        raise HTTPException(status_code=409, detail="ticket_closed")
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > ticket_media.MAX_BYTES:
            raise HTTPException(status_code=413, detail="ticket_file_too_large")
        data.extend(chunk)
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    clean, mime, name = ticket_media.validate(bytes(data), mime)
    await ticket_media.quota(db, ticket, len(clean))
    raw_caption = request.headers.get("x-ticket-caption", "")
    caption = unquote(raw_caption).strip()[:1000] if raw_caption else None
    try:
        message = await tickets.add_message(
            db,
            ticket,
            author_type="staff",
            author_id=ctx.staff.id,
            text=caption or None,
            attachment_kind="photo" if mime.startswith("image/") else "document",
        )
    except tickets.TicketError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    message.attachment_data, message.attachment_size = clean, len(clean)
    message.attachment_mime, message.attachment_name = mime, name
    await queue.enqueue(
        db,
        "notify",
        "ticket_reply",
        {"ticket_id": ticket.id, "message_id": message.id},
        shop_id=ctx.shop_id,
    )
    return {"id": message.id, "ticket": _ticket_row(ticket)}


# --- the help desk: departments, priorities, assignees, notes, canned replies (F-14) ------------

Replier = Annotated[StaffContext, Depends(require("tickets.reply"))]
DeskAdmin = Annotated[StaffContext, Depends(require("settings.write"))]
DEPARTMENT_FIELDS = ["id", "title", "sort", "is_active", "log_chat_id", "log_thread_id"]
CANNED_FIELDS = ["id", "title", "text", "department_id", "sort"]


async def _staff_ticket(db: DB, ticket_id: int, ctx: StaffContext) -> Ticket:
    """Desk actions belong to the shop's staff; a reseller only writes on its own tickets."""
    if await reseller_customer_id(db, ctx) is not None:
        raise HTTPException(status_code=404, detail="not_found")
    return await _ticket(db, ticket_id, ctx)


class TicketUpdateBody(BaseModel):
    department_id: int | None = None
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    assigned_staff_id: int | None = None
    is_complaint: bool | None = None


@router.patch("/tickets/{ticket_id}")
async def update_ticket(ticket_id: int, body: TicketUpdateBody, ctx: Replier, db: DB) -> dict[str, Any]:
    ticket = await _staff_ticket(db, ticket_id, ctx)
    sent = body.model_fields_set
    if body.assigned_staff_id is not None:
        assignee = await db.get(Staff, body.assigned_staff_id)
        if (
            assignee is None
            or assignee.shop_id != ctx.shop_id
            or not assignee.is_active
            or rbac.is_scoped(assignee.role)
        ):
            raise HTTPException(status_code=404, detail="staff_not_found")
    before = _ticket_row(ticket)
    try:
        changed = await tickets.update(
            db,
            ticket,
            department_id=body.department_id,
            clear_department="department_id" in sent and body.department_id is None,
            priority=body.priority,
            assigned_staff_id=body.assigned_staff_id,
            unassign="assigned_staff_id" in sent and body.assigned_staff_id is None,
            complaint=body.is_complaint,
        )
    except tickets.TicketError as exc:
        raise HTTPException(status_code=422, detail=exc.key) from exc
    if changed:
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action="ticket.update",
            entity="ticket",
            entity_id=ticket.id,
            before={key: before.get(key) for key in changed},
            after=changed,
        )
    return {"ticket": _ticket_row(ticket)}


class NoteBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/tickets/{ticket_id}/note")
async def add_ticket_note(ticket_id: int, body: NoteBody, ctx: Replier, db: DB) -> dict[str, Any]:
    ticket = await _staff_ticket(db, ticket_id, ctx)
    note = await tickets.add_message(
        db,
        ticket,
        author_type="staff",
        author_id=ctx.staff.id if ctx.staff else None,
        text=body.text,
        internal=True,
    )
    return {"id": note.id}


class DepartmentBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    sort: int = 0
    is_active: bool = True
    log_chat_id: int | None = None
    log_thread_id: int | None = Field(default=None, ge=1)


@router.get("/ticket-departments")
async def list_ticket_departments(
    ctx: Annotated[StaffContext, Depends(require("tickets.read"))], db: DB
) -> dict[str, Any]:
    rows = await tickets.departments(db, ctx.shop_id, active_only=False)
    return {"items": [plain(row, DEPARTMENT_FIELDS) for row in rows]}


@router.post("/ticket-departments")
async def create_ticket_department(body: DepartmentBody, ctx: DeskAdmin, db: DB) -> dict[str, Any]:
    row = TicketDepartment(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="ticket_department.create",
        entity="ticket_department",
        entity_id=row.id,
        after=body.model_dump(),
    )
    return plain(row, DEPARTMENT_FIELDS)


@router.put("/ticket-departments/{department_id}")
async def update_ticket_department(
    department_id: int, body: DepartmentBody, ctx: DeskAdmin, db: DB
) -> dict[str, Any]:
    row = await owned(db, TicketDepartment, department_id, ctx)
    before = plain(row, DEPARTMENT_FIELDS)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="ticket_department.update",
        entity="ticket_department",
        entity_id=row.id,
        before=before,
        after=body.model_dump(),
    )
    return plain(row, DEPARTMENT_FIELDS)


@router.delete("/ticket-departments/{department_id}")
async def delete_ticket_department(department_id: int, ctx: DeskAdmin, db: DB) -> dict[str, bool]:
    row = await owned(db, TicketDepartment, department_id, ctx)
    await db.delete(row)  # its tickets stay, without a department; its canned replies become shared
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="ticket_department.delete",
        entity="ticket_department",
        entity_id=department_id,
    )
    return {"ok": True}


class CannedBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4000)
    department_id: int | None = None
    sort: int = 0


async def _check_department(db: DB, ctx: StaffContext, department_id: int | None) -> None:
    if department_id is not None:
        await owned(db, TicketDepartment, department_id, ctx)


@router.get("/canned-replies")
async def list_canned_replies(ctx: Replier, db: DB, department_id: int | None = None) -> dict[str, Any]:
    if await reseller_customer_id(db, ctx) is not None:
        raise HTTPException(status_code=404, detail="not_found")
    rows = await tickets.canned_replies(db, ctx.shop_id, department_id)
    return {"items": [plain(row, CANNED_FIELDS) for row in rows]}


@router.post("/canned-replies")
async def create_canned_reply(body: CannedBody, ctx: DeskAdmin, db: DB) -> dict[str, Any]:
    await _check_department(db, ctx, body.department_id)
    row = CannedReply(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="canned_reply.create",
        entity="canned_reply",
        entity_id=row.id,
        after={"title": body.title, "department_id": body.department_id},
    )
    return plain(row, CANNED_FIELDS)


@router.put("/canned-replies/{reply_id}")
async def update_canned_reply(reply_id: int, body: CannedBody, ctx: DeskAdmin, db: DB) -> dict[str, Any]:
    row = await owned(db, CannedReply, reply_id, ctx)
    await _check_department(db, ctx, body.department_id)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="canned_reply.update",
        entity="canned_reply",
        entity_id=row.id,
        after={"title": body.title, "department_id": body.department_id},
    )
    return plain(row, CANNED_FIELDS)


@router.delete("/canned-replies/{reply_id}")
async def delete_canned_reply(reply_id: int, ctx: DeskAdmin, db: DB) -> dict[str, bool]:
    row = await owned(db, CannedReply, reply_id, ctx)
    await db.delete(row)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="canned_reply.delete",
        entity="canned_reply",
        entity_id=reply_id,
    )
    return {"ok": True}


@router.get("/tickets/{ticket_id}/messages/{message_id}/attachment")
async def ticket_attachment(
    ticket_id: int,
    message_id: int,
    ctx: Annotated[StaffContext, Depends(require("tickets.read"))],
    db: DB,
):
    from paurubot.api.routes.webapp_ticket_media import media_response

    ticket = await _ticket(db, ticket_id, ctx)
    staff_view = await reseller_customer_id(db, ctx) is None
    return await media_response(db, ticket, message_id, internal=staff_view)
