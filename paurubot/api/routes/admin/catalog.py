"""Locations, categories and plans (CAT-01, CAT-02)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from paurubot.api.admin_common import owned, plain
from paurubot.api.deps import DB, StaffContext, require
from paurubot.audit.log import record, snapshot
from paurubot.db.models import Category, Location, Panel, Plan, PlanAudience, Service
from paurubot.panels import PanelError, registry

router = APIRouter(prefix="/api/admin", tags=["admin"])
Reader = Annotated[StaffContext, Depends(require("catalog.read"))]
Writer = Annotated[StaffContext, Depends(require("catalog.write"))]

LOCATION_FIELDS = ["id", "title", "flag", "sort", "is_active"]
CATEGORY_FIELDS = ["id", "title", "sort", "is_active"]
PLAN_FIELDS = [
    "id",
    "title",
    "description",
    "category_id",
    "location_id",
    "panel_id",
    "price",
    "days",
    "data_gb",
    "ip_limit",
    "audience",
    "renewable",
    "sort",
    "is_active",
    "button_style",
    "on_hold",
    "panel_options",
    "username_prefix",
    "addons",
]


class LocationBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    flag: str | None = Field(default=None, max_length=16)
    sort: int = 0
    is_active: bool = True


class CategoryBody(BaseModel):
    title: str = Field(min_length=1, max_length=64)
    sort: int = 0
    is_active: bool = True


class PlanPanelOptions(BaseModel):
    """Overrides of the panel's settings for this plan's services; empty means "use the panel's"."""

    group_ids: list[int] = Field(default_factory=list, max_length=50)
    inbounds: list[str] = Field(default_factory=list, max_length=50)
    service_ids: list[int] = Field(default_factory=list, max_length=50)
    template_id: int | None = Field(default=None, ge=1)
    inbound_id: int | None = Field(default=None, ge=1)
    flow: str | None = Field(default=None, max_length=40, pattern=r"^[a-z0-9-]*$")
    limit_ip: int | None = Field(default=None, ge=0, le=100)


class PlanAddons(BaseModel):
    extras: bool = True
    extra_gb_price: int | None = Field(default=None, ge=0, le=10**9)
    extra_day_price: int | None = Field(default=None, ge=0, le=10**9)


class ButtonStyle(BaseModel):
    emoji: str = Field(default="", max_length=8)
    badge: Literal["", "hot", "new", "best", "sale"] = ""


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


class PlanBody(BaseModel):
    title: str = Field(min_length=1, max_length=96)
    description: str | None = Field(default=None, max_length=1000)
    category_id: int | None = None
    location_id: int | None = None
    panel_id: int | None = None
    price: int = Field(ge=0, le=10**11)
    days: int = Field(ge=0, le=3650)
    data_gb: int = Field(ge=0, le=100_000)
    ip_limit: int = Field(default=0, ge=0, le=100)
    audience: PlanAudience = PlanAudience.ALL
    renewable: bool = True
    sort: int = 0
    is_active: bool = True
    on_hold: bool = False
    username_prefix: str | None = Field(default=None, max_length=16, pattern=r"^[a-z0-9_]*$")
    panel_options: PlanPanelOptions = Field(default_factory=PlanPanelOptions)
    addons: PlanAddons = Field(default_factory=PlanAddons)
    button_style: ButtonStyle = Field(default_factory=ButtonStyle)

    def values(self) -> dict[str, Any]:
        data = self.model_dump()
        data["panel_options"] = _compact(data["panel_options"])
        data["addons"] = {k: v for k, v in data["addons"].items() if v is not None}
        data["button_style"] = _compact(data["button_style"])
        data["username_prefix"] = data["username_prefix"] or None
        return data


@router.get("/plans/panel-options/{panel_id}")
async def plan_panel_options(panel_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    """Groups or inbounds a plan can pick from on one panel (PNL-14)."""
    panel = await owned(db, Panel, panel_id, ctx)
    try:
        driver = await registry.driver_for(panel)
        options = [{"id": o.id, "title": o.title} for o in await driver.list_options()]
    except PanelError as exc:
        return {"ok": False, "kind": panel.kind.value, "error": str(exc)[:255], "options": []}
    return {"ok": True, "kind": panel.kind.value, "options": options}


@router.get("/catalog")
async def catalog(ctx: Reader, db: DB) -> dict[str, Any]:
    shop = ctx.shop_id
    locations = await db.scalars(
        select(Location).where(Location.shop_id == shop).order_by(Location.sort, Location.id)
    )
    categories = await db.scalars(
        select(Category).where(Category.shop_id == shop).order_by(Category.sort, Category.id)
    )
    plans = await db.scalars(select(Plan).where(Plan.shop_id == shop).order_by(Plan.sort, Plan.id))
    counts = dict(
        (
            await db.execute(
                select(Service.plan_id, func.count(Service.id))
                .where(Service.shop_id == shop)
                .group_by(Service.plan_id)
            )
        ).all()
    )
    plan_rows = []
    for plan in plans:
        data = plain(plan, PLAN_FIELDS)
        data["services"] = int(counts.get(plan.id, 0))
        plan_rows.append(data)
    return {
        "locations": [plain(x, LOCATION_FIELDS) for x in locations],
        "categories": [plain(x, CATEGORY_FIELDS) for x in categories],
        "plans": plan_rows,
    }


async def _check_refs(db: DB, ctx: StaffContext, body: PlanBody) -> None:
    for model, value in ((Category, body.category_id), (Location, body.location_id), (Panel, body.panel_id)):
        if value is not None:
            await owned(db, model, value, ctx)
    if body.panel_id is None and body.location_id is None:
        raise HTTPException(status_code=422, detail="plan_needs_panel_or_location")


@router.post("/locations")
async def create_location(body: LocationBody, ctx: Writer, db: DB) -> dict[str, Any]:
    row = Location(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="location.create",
        entity="location",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, LOCATION_FIELDS)


@router.put("/locations/{row_id}")
async def update_location(row_id: int, body: LocationBody, ctx: Writer, db: DB) -> dict[str, Any]:
    row = await owned(db, Location, row_id, ctx)
    before = snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="location.update",
        entity="location",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, LOCATION_FIELDS)


@router.post("/categories")
async def create_category(body: CategoryBody, ctx: Writer, db: DB) -> dict[str, Any]:
    row = Category(shop_id=ctx.shop_id, **body.model_dump())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="category.create",
        entity="category",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, CATEGORY_FIELDS)


@router.put("/categories/{row_id}")
async def update_category(row_id: int, body: CategoryBody, ctx: Writer, db: DB) -> dict[str, Any]:
    row = await owned(db, Category, row_id, ctx)
    before = snapshot(row)
    for key, value in body.model_dump().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="category.update",
        entity="category",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, CATEGORY_FIELDS)


@router.post("/plans")
async def create_plan(body: PlanBody, ctx: Writer, db: DB) -> dict[str, Any]:
    await _check_refs(db, ctx, body)
    row = Plan(shop_id=ctx.shop_id, **body.values())
    db.add(row)
    await db.flush()
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="plan.create",
        entity="plan",
        entity_id=row.id,
        after=snapshot(row),
    )
    return plain(row, PLAN_FIELDS)


@router.put("/plans/{row_id}")
async def update_plan(row_id: int, body: PlanBody, ctx: Writer, db: DB) -> dict[str, Any]:
    row = await owned(db, Plan, row_id, ctx)
    await _check_refs(db, ctx, body)
    before = snapshot(row)
    for key, value in body.values().items():
        setattr(row, key, value)
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="plan.update",
        entity="plan",
        entity_id=row.id,
        before=before,
        after=snapshot(row),
    )
    return plain(row, PLAN_FIELDS)


@router.delete("/plans/{row_id}")
async def archive_plan(row_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    """Plans are archived, never deleted: services and orders keep pointing at them."""
    row = await owned(db, Plan, row_id, ctx)
    row.is_active = False
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="plan.archive", entity="plan", entity_id=row.id
    )
    return plain(row, PLAN_FIELDS)
