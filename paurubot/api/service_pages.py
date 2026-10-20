"""Owned keyset pages; filters apply before the limit, never to a loaded client subset."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Service, ServiceStatus


async def read(
    session: AsyncSession,
    shop_id: int,
    customer_id: int,
    *,
    limit: int | None,
    before: int | None,
    query: str | None,
    status: ServiceStatus | None,
) -> tuple[list[Service], int | None]:
    stmt = select(Service).where(
        Service.shop_id == shop_id,
        Service.customer_id == customer_id,
        Service.status != ServiceStatus.DELETED,
    )
    if query and (text := query.strip()):
        # Contains search is literal, not a user-controlled SQL LIKE pattern.
        text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{text}%"
        stmt = stmt.where(
            or_(Service.label.ilike(pattern, escape="\\"), Service.panel_username.ilike(pattern, escape="\\"))
        )
    if status is not None:
        stmt = stmt.where(Service.status == status)
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
