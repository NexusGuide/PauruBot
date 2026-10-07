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
    "delivery",
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
    squad_ids: list[str] = Field(default_factory=list, max_length=50)
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
    style: Literal["", "primary", "success", "danger"] = ""
    emoji_id: str = Field(default="", max_length=25, pattern=r"^\d*$")


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
    delivery: Literal["panel", "stock"] = "panel"

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
    from paurubot.core import stock

    plan_list = list(plans)
    stocked = await stock.counts(db, [p.id for p in plan_list if p.delivery == "stock"])
    plan_rows = []
    for plan in plan_list:
        data = plain(plan, PLAN_FIELDS)
        data["services"] = int(counts.get(plan.id, 0))
        if plan.id in stocked:
            data["stock"] = stocked[plan.id]
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
    if body.delivery == "stock":
        if body.location_id is None:
            raise HTTPException(status_code=422, detail="stock_plan_needs_location")
        return
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


# ---------------------------------------------------------------- ready-made config stock (CAT-05)


class StockBody(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    note: str | None = Field(default=None, max_length=128)


def _preview(text: str) -> str:
    return text if len(text) <= 32 else text[:24] + "…" + text[-6:]


async def _stock_plan(db: DB, ctx: StaffContext, plan_id: int) -> Plan:
    plan = await owned(db, Plan, plan_id, ctx)
    if plan.delivery != "stock":
        raise HTTPException(status_code=409, detail="not_a_stock_plan")
    return plan


@router.get("/plans/{plan_id}/stock")
async def list_stock(plan_id: int, ctx: Writer, db: DB, status: str | None = None) -> dict[str, Any]:
    from paurubot.core import stock
    from paurubot.db.models import Customer, StockItem

    plan = await _stock_plan(db, ctx, plan_id)
    stmt = select(StockItem).where(StockItem.plan_id == plan.id).order_by(StockItem.id.desc()).limit(200)
    if status in {stock.AVAILABLE, stock.SOLD, stock.DISABLED}:
        stmt = stmt.where(StockItem.status == status)
    items = []
    for item in await db.scalars(stmt):
        customer = await db.get(Customer, item.customer_id) if item.customer_id else None
        items.append(
            {
                "id": item.id,
                "status": item.status,
                "note": item.note,
                "preview": _preview(stock.payload(item)),
                "created_at": item.created_at.isoformat(),
                "sold_at": item.sold_at.isoformat() if item.sold_at else None,
                "customer_id": item.customer_id,
                "customer": customer.display_name if customer else None,
                "order_id": item.order_id,
            }
        )
    return {"counts": (await stock.counts(db, [plan.id]))[plan.id], "items": items}


@router.post("/plans/{plan_id}/stock")
async def add_stock(plan_id: int, body: StockBody, ctx: Writer, db: DB) -> dict[str, Any]:
    from paurubot.core import stock

    plan = await _stock_plan(db, ctx, plan_id)
    try:
        result = await stock.add(db, plan, body.text, body.note)
    except stock.StockError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await record(
        db,
        shop_id=ctx.shop_id,
        actor=ctx.actor,
        action="stock.add",
        entity="plan",
        entity_id=plan.id,
        after={"added": result.added, "duplicates": result.duplicates, "too_long": result.too_long},
    )
    return {"added": result.added, "duplicates": result.duplicates, "too_long": result.too_long}


@router.get("/stock/{item_id}")
async def show_stock_item(item_id: int, ctx: Writer, db: DB) -> dict[str, Any]:
    """The full config, for support. Every look is audited."""
    from paurubot.core import stock
    from paurubot.db.models import StockItem

    item = await owned(db, StockItem, item_id, ctx)
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="stock.view", entity="stock", entity_id=item.id
    )
    return {"id": item.id, "status": item.status, "payload": stock.payload(item)}


@router.delete("/stock/{item_id}")
async def delete_stock_item(item_id: int, ctx: Writer, db: DB) -> dict[str, bool]:
    from paurubot.core import stock
    from paurubot.db.models import StockItem

    item = await owned(db, StockItem, item_id, ctx)
    if item.status == stock.SOLD:
        raise HTTPException(status_code=409, detail="stock_sold")
    await db.delete(item)
    await record(
        db, shop_id=ctx.shop_id, actor=ctx.actor, action="stock.delete", entity="stock", entity_id=item_id
    )
    return {"ok": True}
