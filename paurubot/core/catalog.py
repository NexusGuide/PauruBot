"""What a customer can buy: locations, categories and plans with audience rules (CAT-01, CAT-02)."""

from __future__ import annotations

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.resale import catalog_owner
from paurubot.db.models import Category, Customer, Location, Panel, Plan, PlanAudience, Service, ServiceStatus


def _audience_filter(stmt: Select, customer: Customer | None) -> Select:  # type: ignore[type-arg]
    if customer is not None and customer.is_reseller:
        return stmt.where(Plan.audience.in_([PlanAudience.ALL, PlanAudience.RESELLERS]))
    return stmt.where(Plan.audience.in_([PlanAudience.ALL, PlanAudience.CUSTOMERS]))


def visible_plans_stmt(shop_id: int, customer: Customer | None) -> Select:  # type: ignore[type-arg]
    """Active plans whose panel (or location pool) is active."""
    panel_ok = select(Panel.id).where(Panel.shop_id == catalog_owner(shop_id), Panel.is_active.is_(True))
    location_ok = select(Panel.location_id).where(
        Panel.shop_id == catalog_owner(shop_id), Panel.is_active.is_(True), Panel.location_id.is_not(None)
    )
    from paurubot.core.stock import available_stmt

    stmt = select(Plan).where(
        Plan.shop_id == catalog_owner(shop_id),
        Plan.is_active.is_(True),
        or_(
            (Plan.delivery == "stock") & available_stmt(Plan.id),
            (Plan.delivery != "stock")
            & or_(Plan.panel_id.in_(panel_ok), (Plan.panel_id.is_(None) & Plan.location_id.in_(location_ok))),
        ),
    )
    return _audience_filter(stmt, customer)


async def locations(session: AsyncSession, shop_id: int, customer: Customer | None) -> list[Location]:
    plan_locations = visible_plans_stmt(shop_id, customer).with_only_columns(Plan.location_id)
    panel_locations = (
        visible_plans_stmt(shop_id, customer)
        .join(Panel, Panel.id == Plan.panel_id)
        .with_only_columns(Panel.location_id)
    )
    stmt = (
        select(Location)
        .where(
            Location.shop_id == catalog_owner(shop_id),
            Location.is_active.is_(True),
            or_(Location.id.in_(plan_locations), Location.id.in_(panel_locations)),
        )
        .order_by(Location.sort, Location.id)
    )
    return list((await session.scalars(stmt)).all())


async def categories(
    session: AsyncSession, shop_id: int, customer: Customer | None, location_id: int | None = None
) -> list[Category]:
    plans_stmt = visible_plans_stmt(shop_id, customer)
    if location_id:
        plans_stmt = _in_location(plans_stmt, shop_id, location_id)
    stmt = (
        select(Category)
        .where(
            Category.shop_id == catalog_owner(shop_id),
            Category.is_active.is_(True),
            Category.id.in_(plans_stmt.with_only_columns(Plan.category_id)),
        )
        .order_by(Category.sort, Category.id)
    )
    return list((await session.scalars(stmt)).all())


def _in_location(stmt: Select, shop_id: int, location_id: int) -> Select:  # type: ignore[type-arg]
    panels_here = select(Panel.id).where(
        Panel.shop_id == catalog_owner(shop_id), Panel.location_id == location_id
    )
    return stmt.where(or_(Plan.location_id == location_id, Plan.panel_id.in_(panels_here)))


async def plans(
    session: AsyncSession,
    shop_id: int,
    customer: Customer | None,
    *,
    location_id: int | None = None,
    category_id: int | None = None,
    uncategorised: bool = False,
    panel_id: int | None = None,
) -> list[Plan]:
    stmt = visible_plans_stmt(shop_id, customer)
    if location_id:
        stmt = _in_location(stmt, shop_id, location_id)
    if category_id:
        stmt = stmt.where(Plan.category_id == category_id)
    elif uncategorised:
        stmt = stmt.where(Plan.category_id.is_(None))
    if panel_id:
        stmt = stmt.where(Plan.panel_id == panel_id)
    stmt = stmt.order_by(Plan.sort, Plan.price, Plan.id)
    return list((await session.scalars(stmt)).all())


async def get_plan(
    session: AsyncSession, shop_id: int, plan_id: int, customer: Customer | None
) -> Plan | None:
    stmt = visible_plans_stmt(shop_id, customer).where(Plan.id == plan_id)
    return await session.scalar(stmt)


async def panel_load(session: AsyncSession, panel_id: int) -> int:
    stmt = select(func.count(Service.id)).where(
        Service.panel_id == panel_id, Service.status.not_in([ServiceStatus.DELETED])
    )
    return int(await session.scalar(stmt) or 0)


async def pick_panel(session: AsyncSession, plan: Plan) -> Panel | None:
    """The plan's own panel, or the least-loaded healthy panel with room in the plan's location."""
    return await pick_panel_for(session, plan.shop_id, plan.panel_id, plan.location_id)


async def pick_panel_for(
    session: AsyncSession, shop_id: int, panel_id: int | None, location_id: int | None
) -> Panel | None:
    from paurubot.provisioning.monitor import quota_full

    if panel_id:
        panel = await session.get(Panel, panel_id)
        if panel is None or panel.shop_id != shop_id or not panel.is_active or quota_full(panel):
            return None
        if panel.capacity and await panel_load(session, panel.id) >= panel.capacity:
            return None
        return panel
    if not location_id:
        return None
    candidates = list(
        await session.scalars(
            select(Panel).where(
                Panel.shop_id == shop_id,
                Panel.location_id == location_id,
                Panel.is_active.is_(True),
                Panel.is_healthy.is_not(False),
            )
        )
    )
    best: tuple[float, Panel] | None = None
    for panel in candidates:
        if quota_full(panel):
            continue
        load = await panel_load(session, panel.id)
        if panel.capacity and load >= panel.capacity:
            continue
        ratio = load / panel.capacity if panel.capacity else load / 1_000_000
        if best is None or ratio < best[0]:
            best = (ratio, panel)
    return best[1] if best else None
