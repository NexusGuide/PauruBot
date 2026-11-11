"""Capacity warning (ClockBot parity): staff hear once when a panel's services reach the shop's
warning percent of the panel's capacity; the warning re-arms after it drops back below."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.core import catalog
from paurubot.core import settings as shop_settings
from paurubot.db.models import Panel
from paurubot.worker import queue

MARK_TTL = 30 * 86400


async def check(session: AsyncSession) -> int:
    """Queue a warning for every panel newly past its shop's threshold; returns how many."""
    queued = 0
    rows = await session.scalars(select(Panel).where(Panel.is_active.is_(True), Panel.capacity.is_not(None)))
    for panel in list(rows):
        percent = int((await shop_settings.load(session, panel.shop_id)).get("capacity_warn_percent") or 0)
        if percent <= 0 or not panel.capacity:
            continue
        mark = f"capacity:{panel.id}"
        over = await catalog.panel_load(session, panel.id) * 100 >= percent * panel.capacity
        warned = bool(await kv.marked([mark]))
        if over and not warned:
            await queue.enqueue(
                session,
                "notify",
                "panel_alert",
                {"panel_id": panel.id, "event": "capacity", "percent": percent},
                shop_id=panel.shop_id,
            )
            await kv.mark(mark, MARK_TTL)
            queued += 1
        elif not over and warned:
            await kv.unmark(mark)
    await session.commit()
    return queued
