"""Limits for reseller-owned broadcast jobs."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.reports import day_bounds
from paurubot.db.base import utcnow
from paurubot.db.models import Broadcast, JobStatus

DAILY_LIMIT = 2


async def sent_today(session: AsyncSession, shop_id: int) -> int:
    start, end = day_bounds(utcnow())
    return int(
        await session.scalar(
            select(func.count(Broadcast.id)).where(
                Broadcast.shop_id == shop_id,
                Broadcast.created_at >= start,
                Broadcast.created_at < end,
                Broadcast.status != JobStatus.CANCELED,
            )
        )
        or 0
    )


async def remaining_today(session: AsyncSession, shop_id: int) -> int:
    return max(0, DAILY_LIMIT - await sent_today(session, shop_id))
