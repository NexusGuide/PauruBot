"""Resellers (TEN-*), staff accounts and support tickets (SUP-01)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from paurubot.api.admin_common import Page, owned, page_params, paginate, plain, reseller_customer_id
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.auth import rbac, sessions
from paurubot.auth import staff as staff_service
from paurubot.core import tickets
from paurubot.db.models import (
    ApplicationStatus,
    Customer,
    ResellerApplication,
    ResellerGroup,
    ResellerProfile,
    Staff,
    StaffRole,
    Ticket,
    TicketStatus,
)
from paurubot.worker import queue

router = APIRouter(prefix="/api/admin", tags=["admin"])
Manager = Annotated[StaffContext, Depends(require("resellers.manage"))]

GROUP_FIELDS = ["id", "title", "discount_percent", "min_topup", "max_active_services", "is_active"]
PROFILE_FIELDS = [
    "id",
    "customer_id",
    "group_id",
    "permissions",
    "is_active",
    "expires_at",
    "max_purchases_per_day",
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
    return {
        "groups": [plain(g, GROUP_FIELDS) for g in groups],
        "profiles": [{**plain(p, PROFILE_FIELDS), "customer": c.display_name} for p, c in profiles.all()],
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


@router.post("/reseller-groups")
async def create_group(body: GroupBody, ctx: Manager, db: DB) -> dict[str, Any]:
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


class ProfileBody(BaseModel):
    customer_id: int
    group_id: int | None = None
    permissions: list[str] = Field(default_factory=list)
    is_active: bool = True
    expires_at: datetime | None = None
    max_purchases_per_day: int | None = Field(default=None, ge=1)


@router.put("/resellers")
async def upsert_profile(body: ProfileBody, ctx: Manager, db: DB) -> dict[str, Any]:
    customer = await owned(db, Customer, body.customer_id, ctx)
    if body.group_id is not None:
        await owned(db, ResellerGroup, body.group_id, ctx)
    unknown = set(body.permissions) - rbac.SCOPED_FOR_RESELLER
    if unknown:
        raise HTTPException(status_code=422, detail="permission_not_allowed_for_resellers")
    profile = await db.scalar(select(ResellerProfile).where(ResellerProfile.customer_id == customer.id))
    before = snapshot(profile) if profile else None
    if profile is None:
        profile = ResellerProfile(shop_id=ctx.shop_id, customer_id=customer.id)
        db.add(profile)
    for key, value in body.model_dump(exclude={"customer_id"}).items():
        setattr(profile, key, value)
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


class DecisionBody(BaseModel):
    approve: bool
    group_id: int | None = None


@router.post("/reseller-applications/{application_id}")
async def decide(application_id: int, body: DecisionBody, ctx: Manager, db: DB) -> dict[str, Any]:
    application = await owned(db, ResellerApplication, application_id, ctx)
    if application.status != ApplicationStatus.PENDING:
        raise HTTPException(status_code=409, detail="already_decided")
    if body.approve:
        if body.group_id is None:
            raise HTTPException(status_code=422, detail="group_required")
        await owned(db, ResellerGroup, body.group_id, ctx)
        profile = await db.scalar(
            select(ResellerProfile).where(ResellerProfile.customer_id == application.customer_id)
        )
        if profile is None:
            db.add(
                ResellerProfile(
                    shop_id=ctx.shop_id,
                    customer_id=application.customer_id,
                    group_id=body.group_id,
                    permissions=sorted(rbac.SCOPED_FOR_RESELLER),
                )
            )
        else:
            profile.group_id, profile.is_active = body.group_id, True
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
        {"customer_id": application.customer_id, "approved": body.approve},
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
                **plain(t, ["id", "subject", "status", "customer_id", "created_at", "updated_at"]),
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
        "ticket": plain(ticket, ["id", "subject", "status", "customer_id", "created_at", "updated_at"]),
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
    try:
        await tickets.add_message(db, ticket, author_type="staff", author_id=ctx.staff.id, text=body.text)
    except tickets.TicketError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    await queue.enqueue(db, "notify", "ticket_reply", {"ticket_id": ticket.id}, shop_id=ctx.shop_id)
    return {"ok": True}


@router.post("/tickets/{ticket_id}/close")
async def close(
    ticket_id: int, ctx: Annotated[StaffContext, Depends(require("tickets.reply"))], db: DB
) -> dict[str, Any]:
    ticket = await _ticket(db, ticket_id, ctx)
    await tickets.close(db, ticket)
    return {"ok": True}
