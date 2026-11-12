"""Owned keyset pages; filters apply before the limit, never to a loaded client subset."""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Location, Panel, Service, ServiceStatus
from paurubot.db.search import ESCAPE, contains

NO_LOCATION = 0  # the group of services whose panel has no location


async def read(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    *,
    limit: int | None,
    before: int | None,
    query: str | None,
    status: ServiceStatus | None,
    group: int | None = None,
) -> tuple[list[Service], int | None]:
    stmt = select(Service).where(
        Service.shop_id == shop_id,
        Service.customer_id == customer_id,
        Service.status != ServiceStatus.DELETED,
    )
    if query and (text := query.strip()):
        # Contains search is literal, not a user-controlled SQL LIKE pattern.
        pattern = contains(text)
        stmt = stmt.where(
            or_(
                Service.label.ilike(pattern, escape=ESCAPE),
                Service.panel_username.ilike(pattern, escape=ESCAPE),
            )
        )
    if status is not None:
        stmt = stmt.where(Service.status == status)
    if group is not None:
        # One location group (F-19), filtered before the limit like every other filter.
        located = select(Panel.id).where(
            Panel.location_id.is_(None) if group == NO_LOCATION else Panel.location_id == group
        )
        stmt = stmt.where(Service.panel_id.in_(located))
    if before is not None:
        stmt = stmt.where(Service.id < before)
    stmt = stmt.order_by(Service.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = list(await session.scalars(stmt))
    more = limit is not None and len(rows) > limit
    if limit is not None:
        rows = rows[:limit]
    return rows, rows[-1].id if more else None


async def groups(session: AsyncSession, shop_id: int, customer_id: int) -> list[dict[str, object]]:
    """The customer's services counted per location (F-19), from the database only.

    Locations, never panel names: which server a service lives on stays the shop's business.
    """
    rows = (
        await session.execute(
            select(Panel.location_id, Location.title, Location.flag, func.count(Service.id))
            .join(Panel, Panel.id == Service.panel_id)
            .outerjoin(Location, Location.id == Panel.location_id)
            .where(
                Service.shop_id == shop_id,
                Service.customer_id == customer_id,
                Service.status != ServiceStatus.DELETED,
            )
            .group_by(Panel.location_id, Location.title, Location.flag)
        )
    ).all()
    out: list[dict[str, object]] = [
        {"id": location_id or NO_LOCATION, "title": title, "flag": flag, "count": int(count)}
        for location_id, title, flag, count in rows
    ]
    # Named locations first, by name; "elsewhere" last.
    return sorted(out, key=lambda g: (g["id"] == NO_LOCATION, str(g["title"] or "")))
