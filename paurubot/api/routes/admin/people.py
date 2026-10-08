"""Resellers (TEN-*), staff accounts and support tickets (SUP-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

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
from paurubot.core.resellers import web_login
from paurubot.db.models import (
    ApplicationStatus,
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
    StaffRole,
    Ticket,
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
                "customer": c.display_name,
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
                "customer": c.display_name,
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
    if profile is None:
        profile = ResellerProfile(shop_id=ctx.shop_id, customer_id=customer.id)
        db.add(profile)
    switching_to_metered = body.billing_mode != "prepaid" and profile.billing_mode == "prepaid"
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
    if profile.id is None:
        return
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


@router.get("/resellers/{profile_id}/billing")
async def reseller_billing(profile_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    return await _billing_view(db, await owned(db, ResellerProfile, profile_id, ctx))


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
    return {
        "max_discount": parent.discount_percent,
        "limit": parent.max_subresellers,
        "items": [
            {
                **plain(r, SUB_FIELDS),
                "customer": names[r.customer_id].display_name if r.customer_id in names else None,
                "telegram_id": names[r.customer_id].telegram_id if r.customer_id in names else None,
                "discount_percent": r.discount_percent,
                "balance": balances.get(r.customer_id, 0),
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
    if body.role == StaffRole.OWNER:
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
    if row.role == StaffRole.OWNER or body.role == StaffRole.OWNER:
        raise HTTPException(status_code=422, detail="owner_is_fixed")
    before = snapshot(row)
    if body.role is not None:
        row.role = body.role
        row.session_version += 1
    if body.is_active is not None:
        row.is_active = body.is_active
        row.session_version += 1
    if body.telegram_id is not None:
        row.telegram_id = body.telegram_id
    if body.display_name is not None:
        row.display_name = body.display_name
    if body.password:
        await staff_service.change_password(db, row, body.password)
    else:
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
) -> dict[str, Any]:
    stmt = select(Ticket).where(Ticket.shop_id == ctx.shop_id)
    owner = await reseller_customer_id(db, ctx)
    if owner is not None:
        stmt = stmt.where(Ticket.customer_id == owner)
    if status:
        stmt = stmt.where(Ticket.status == status)
    rows, total = await paginate(db, stmt.order_by(Ticket.updated_at.desc()), page)
    names = {
        c.id: c.display_name
        for c in await db.scalars(select(Customer).where(Customer.id.in_([t.customer_id for t in rows])))
    }
    return {
        "items": [
            {
                **plain(t, ["id", "subject", "status", "source", "customer_id", "created_at", "updated_at"]),
                "customer": names.get(t.customer_id),
            }
            for t in rows
        ],
        "total": total,
    }


async def _ticket(db: DB, ticket_id: int, ctx: StaffContext) -> Ticket:
    ticket = await owned(db, Ticket, ticket_id, ctx)
    owner = await reseller_customer_id(db, ctx)
    if owner is not None and ticket.customer_id != owner:
        raise HTTPException(status_code=404, detail="not_found")
    return ticket


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int, ctx: Annotated[StaffContext, Depends(require("tickets.read"))], db: DB
) -> dict[str, Any]:
    ticket = await _ticket(db, ticket_id, ctx)
    customer = await db.get(Customer, ticket.customer_id)
    messages = await tickets.messages(db, ticket.id, limit=200)
    return {
        "ticket": plain(
            ticket, ["id", "subject", "status", "source", "customer_id", "created_at", "updated_at"]
        ),
        "customer": customer.display_name if customer else None,
        "messages": [
            plain(m, ["id", "author_type", "author_id", "text", "attachment_kind", "created_at"])
            | {"has_attachment": bool(m.attachment_file_id)}
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
        ticket = await tickets.open_ticket(db, customer, body.subject, source="panel")
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
    await tickets.close(db, ticket)
    return {"ok": True}
