"""Panel admin accounts for sale and the accounts sold (TEN-14)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import ColumnElement, or_, select

from paurubot.api.admin_common import owned, plain
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.core import panel_accounts
from paurubot.db.models import AccountPlan, Customer, Panel, PanelAccount, PanelKind, PlanAudience
from paurubot.panels import PanelError, registry
from paurubot.panels.drivers.pasarguard import PasarGuardDriver

router = APIRouter(prefix="/api/admin", tags=["admin"])
Manager = Annotated[StaffContext, Depends(require("resellers.manage"))]

PLAN_FIELDS = [
    "id",
    "panel_id",
    "title",
    "description",
    "role_id",
    "data_gb",
    "max_users",
    "days",
    "price",
    "gb_price",
    "user_price",
    "day_price",
    "gb_min",
    "gb_max",
    "days_min",
    "days_max",
    "renew_packages_enabled",
    "renew_packages",
    "renew_custom_enabled",
    "renew_mode",
    "renewable",
    "audience",
    "discounts_allowed",
    "reseller_discount",
    "sort",
    "is_active",
]
ACCOUNT_FIELDS = [
    "id",
    "customer_id",
    "panel_id",
    "plan_id",
    "username",
    "data_limit",
    "max_users",
    "expire_at",
    "status",
    "used_bytes",
    "users_count",
    "panel_status",
    "synced_at",
    "note",
    "created_at",
]


class AccountPlanBody(BaseModel):
    panel_id: int
    title: str = Field(min_length=1, max_length=96)
    description: str | None = Field(default=None, max_length=2000)
    role_id: int = Field(ge=1)
    data_gb: int = Field(default=0, ge=0, le=panel_accounts.MAX_GB)
    max_users: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_USERS)
    days: int = Field(default=30, ge=0, le=3650)
    price: int = Field(ge=0, le=10**12)
    gb_price: int = Field(default=0, ge=0, le=10**10)
    user_price: int = Field(default=0, ge=0, le=10**10)
    day_price: int = Field(default=0, ge=0, le=10**10)
    # None: the shop's range from Settings.
    gb_min: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_GB)
    gb_max: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_GB)
    days_min: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_DAYS)
    days_max: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_DAYS)
    renew_packages_enabled: bool = False
    renew_packages: list[dict[str, Any]] = Field(default_factory=list)
    renew_custom_enabled: bool = False
    # None: the original rule (time added, a fresh quota, usage reset).
    renew_mode: Literal["extend_reset", "extend_carry", "restart_reset", "restart_carry"] | None = None
    renewable: bool = True
    audience: PlanAudience = PlanAudience.ALL
    discounts_allowed: bool = False
    reseller_discount: bool = False
    sort: int = 0
    is_active: bool = True

    @field_validator("renew_packages")
    @classmethod
    def _packages(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return panel_accounts.clean_packages(value)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _ranges(self) -> AccountPlanBody:
        for unit in ("gb", "days"):
            low, high = getattr(self, f"{unit}_min"), getattr(self, f"{unit}_max")
            if low is not None and high is not None and low > high:
                raise ValueError("account_extras_invalid")
        return self


async def _pasarguard(db: DB, ctx: StaffContext, panel_id: int) -> Panel:
    panel = await owned(db, Panel, panel_id, ctx)
    if panel.kind != PanelKind.PASARGUARD:
        raise HTTPException(status_code=422, detail="account_panel_not_pasarguard")
    return panel


@router.get("/account-plans")
async def account_plans(ctx: Manager, db: DB) -> dict[str, Any]:
    rows = await db.scalars(
        select(AccountPlan)
        .where(AccountPlan.shop_id == ctx.shop_id)
        .order_by(AccountPlan.sort, AccountPlan.id)
    )
    panels = await db.scalars(
        select(Panel).where(Panel.shop_id == ctx.shop_id, Panel.kind == PanelKind.PASARGUARD)
    )
    return {
        "items": [plain(x, PLAN_FIELDS) for x in rows],
        "panels": [{"id": p.id, "name": p.name, "is_active": p.is_active} for p in panels],
    }


@router.get("/account-plans/roles/{panel_id}")
async def account_roles(panel_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    """Admin roles on a PasarGuard panel a plan can give."""
    panel = await _pasarguard(db, ctx, panel_id)
    try:
        driver = await registry.driver_for(panel)
        assert isinstance(driver, PasarGuardDriver)
        roles = [{"id": o.id, "title": o.title} for o in await driver.admin_roles()]
    except PanelError as exc:
        return {"ok": False, "error": str(exc)[:255], "roles": []}
    return {"ok": True, "roles": roles}


@router.post("/account-plans")
async def create_account_plan(body: AccountPlanBody, ctx: Manager, db: DB) -> dict[str, Any]:
    await _pasarguard(db, ctx, body.panel_id)
    row = AccountPlan(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="account_plan.create",
        entity="account_plan",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, PLAN_FIELDS)


@router.put("/account-plans/{row_id}")
async def update_account_plan(row_id: int, body: AccountPlanBody, ctx: Manager, db: DB) -> dict[str, Any]:
    row = await owned(db, AccountPlan, row_id, ctx)
    await _pasarguard(db, ctx, body.panel_id)
    before = snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="account_plan.update",
        entity="account_plan",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, PLAN_FIELDS)


@router.delete("/account-plans/{row_id}")
async def archive_account_plan(row_id: int, ctx: Manager, db: DB) -> dict[str, Any]:
    """Archived, never deleted: sold accounts keep pointing at their plan."""
    row = await owned(db, AccountPlan, row_id, ctx)
    row.is_active = False
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="account_plan.archive",
        entity="account_plan",
        entity_id=row.id,
    )
    return plain(row, PLAN_FIELDS)


def _account_row(account: PanelAccount, customer: Customer | None) -> dict[str, Any]:
    out = plain(account, ACCOUNT_FIELDS)
    out["customer"] = customer.display_name if customer else None
    out["telegram_id"] = customer.telegram_id if customer else None
    out["plan"] = account.plan.title if account.plan else None
    out["panel"] = account.panel.name if account.panel else None
    return out


@router.get("/panel-accounts")
async def list_panel_accounts(
    ctx: Manager,
    db: DB,
    q: str | None = Query(default=None, max_length=64),
    customer_id: int | None = None,
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    query = (
        select(PanelAccount, Customer)
        .join(Customer, Customer.id == PanelAccount.customer_id)
        .where(PanelAccount.shop_id == ctx.shop_id)
    )
    if customer_id is not None:
        query = query.where(PanelAccount.customer_id == customer_id)
    if status:
        query = query.where(PanelAccount.status == status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        filters: list[ColumnElement[bool]] = [
            PanelAccount.username.ilike(like),
            Customer.username.ilike(like),
        ]
        if q.strip().isdigit():
            filters.append(Customer.telegram_id == int(q.strip()))
        query = query.where(or_(*filters))
    rows = await db.execute(query.order_by(PanelAccount.id.desc()).limit(limit).offset(offset))
    return {"items": [_account_row(a, c) for a, c in rows.all()]}


class AccountChange(BaseModel):
    """A staff change to one sold account; `notify` tells its owner, with the reason."""

    action: Literal["gift", "reset_usage", "password", "users", "plan"]
    days: int = Field(default=0, ge=0, le=panel_accounts.MAX_DAYS)
    gb: int = Field(default=0, ge=0, le=panel_accounts.MAX_GB)
    max_users: int | None = Field(default=None, ge=1, le=panel_accounts.MAX_USERS)
    plan_id: int | None = None
    reason: str | None = Field(default=None, max_length=255)
    notify: bool = True


async def _tell_owner(
    db: DB, account: PanelAccount, change: str, reason: str | None, *, login: bool = False
) -> None:
    from paurubot.db.base import utcnow
    from paurubot.worker import queue

    await queue.enqueue(
        db,
        "notify",
        "account_changed",
        {"account_id": account.id, "change": change, "reason": reason or "", "login": login},
        shop_id=account.shop_id,
        dedupe_key=f"account:{account.id}:staff:{change}:{utcnow():%Y%m%d%H%M%S%f}",
    )


@router.post("/panel-accounts/{row_id}/change")
async def change_panel_account(row_id: int, body: AccountChange, ctx: Manager, db: DB) -> dict[str, Any]:
    account = await owned(db, PanelAccount, row_id, ctx)
    before = plain(account, ACCOUNT_FIELDS)
    try:
        if body.action == "gift":
            if not body.days and not body.gb:
                raise HTTPException(status_code=422, detail="gift_empty")
            await panel_accounts.gift(db, account, days=body.days, gb=body.gb)
        elif body.action == "reset_usage":
            await panel_accounts.reset_usage(db, account)
        elif body.action == "password":
            await panel_accounts.change_password(db, account)
        elif body.action == "users":
            await panel_accounts.set_max_users(db, account, body.max_users)
        else:
            plan = await owned(db, AccountPlan, body.plan_id or 0, ctx)
            await panel_accounts.set_plan(db, account, plan)
    except panel_accounts.AccountError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    except panel_accounts.ProvisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    after = plain(account, ACCOUNT_FIELDS)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action=f"panel_account.{body.action}",
        entity="panel_account",
        entity_id=account.id,
        before={k: v for k, v in before.items() if after.get(k) != v},
        after={
            **{k: v for k, v in after.items() if before.get(k) != v},
            **({"reason": body.reason} if body.reason else {}),
        },
    )
    # A new password always reaches the owner: nobody else may hold it.
    if body.notify or body.action == "password":
        await _tell_owner(db, account, body.action, body.reason, login=body.action == "password")
    customer = await db.get(Customer, account.customer_id)
    return {"item": _account_row(account, customer)}


class AccountNote(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


@router.put("/panel-accounts/{row_id}/note")
async def note_panel_account(row_id: int, body: AccountNote, ctx: Manager, db: DB) -> dict[str, Any]:
    account = await owned(db, PanelAccount, row_id, ctx)
    account.note = (body.note or "").strip() or None
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel_account.note",
        entity="panel_account",
        entity_id=account.id,
    )
    customer = await db.get(Customer, account.customer_id)
    return {"item": _account_row(account, customer)}


class AccountGift(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    days: int = Field(default=0, ge=0, le=panel_accounts.MAX_DAYS)
    gb: int = Field(default=0, ge=0, le=panel_accounts.MAX_GB)
    reason: str | None = Field(default=None, max_length=255)
    notify: bool = True


@router.post("/panel-accounts/bulk/gift")
async def gift_panel_accounts(body: AccountGift, ctx: Manager, db: DB) -> dict[str, Any]:
    """Free days and traffic for many accounts at once (an outage, a festival)."""
    if not body.days and not body.gb:
        raise HTTPException(status_code=422, detail="gift_empty")
    done: list[int] = []
    failed: list[dict[str, Any]] = []
    for row_id in dict.fromkeys(body.ids):
        account = await db.get(PanelAccount, row_id)
        if account is None or account.shop_id != ctx.shop_id:
            failed.append({"id": row_id, "error": "not_found"})
            continue
        try:
            await panel_accounts.gift(db, account, days=body.days, gb=body.gb)
        except panel_accounts.AccountError as exc:
            failed.append({"id": row_id, "error": exc.key})
            continue
        except (panel_accounts.ProvisionError, PanelError):
            failed.append({"id": row_id, "error": "panel_error"})
            continue
        done.append(account.id)
        if body.notify:
            await _tell_owner(db, account, "gift", body.reason)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="panel_account.bulk_gift",
        entity="panel_account",
        after={"ids": done, "days": body.days, "gb": body.gb, "reason": body.reason},
    )
    return {"done": done, "failed": failed}


class AccountGive(BaseModel):
    customer_id: int
    plan_id: int


@router.post("/panel-accounts")
async def give_panel_account(body: AccountGive, ctx: Manager, db: DB) -> dict[str, Any]:
    """An admin account for a customer, free, through the normal provisioning queue."""
    from datetime import UTC, datetime

    from paurubot.db.models import Order, OrderKind, OrderStatus
    from paurubot.worker import queue

    if ctx.scoped:
        raise HTTPException(status_code=403, detail="forbidden")
    customer = await owned(db, Customer, body.customer_id, ctx)
    plan = await owned(db, AccountPlan, body.plan_id, ctx)
    await _pasarguard(db, ctx, plan.panel_id)
    order = Order(
        shop_id=ctx.shop_id,
        customer_id=customer.id,
        kind=OrderKind.ACCOUNT_PURCHASE,
        status=OrderStatus.PAID,
        params={"account_plan_id": plan.id},
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
        action="panel_account.give",
        entity="order",
        entity_id=order.id,
        after={"customer_id": customer.id, "plan_id": plan.id},
    )
    return {"order_id": order.id}


@router.post("/panel-accounts/{row_id}/{action}")
async def act_on_panel_account(
    row_id: int, action: Literal["pause", "resume", "refresh", "delete"], ctx: Manager, db: DB
) -> dict[str, Any]:
    account = await owned(db, PanelAccount, row_id, ctx)
    try:
        if action == "refresh":
            await panel_accounts.refresh(db, account)
        elif action == "delete":
            await panel_accounts.remove(db, account)
        else:
            await panel_accounts.set_paused(db, account, action == "pause")
    except panel_accounts.AccountError as exc:
        raise HTTPException(status_code=409, detail=exc.key) from exc
    except panel_accounts.ProvisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PanelError as exc:
        raise HTTPException(status_code=502, detail="panel_error") from exc
    if action != "refresh":
        await record(
            db,
            shop_id=ctx.shop_id,
            actor=ctx.actor,
            action=f"panel_account.{action}",
            entity="panel_account",
            entity_id=account.id,
        )
    customer = await db.get(Customer, account.customer_id)
    return {"item": _account_row(account, customer)}
