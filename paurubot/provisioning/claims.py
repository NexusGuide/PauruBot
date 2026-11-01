"""Durable ownership of panel account names by orders (SEC-F03).

A name is claimed in the same commit that stores it on the order. The unique (panel, username)
claim means two orders can never pick the same account name on a panel, even before either
has a service row. Adoption is strict: an account found on the panel belongs to this order
only if this order recorded a create request for exactly that name (``create_state``). Any
other account under the name (made by staff, another bot or an earlier install) is someone
else's. It is never handed over; the order claims a new name instead.
"""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core.usernames import MAX_LEN
from paurubot.db.base import utcnow
from paurubot.db.models import PanelUsernameClaim, Service

MAX_TRIES = 5


async def claim(session: AsyncSession, panel_id: int, username: str, order_id: int) -> bool:
    """Claim `username` on the panel for `order_id`. True if this order holds it now."""
    used = await session.scalar(
        select(Service.id).where(Service.panel_id == panel_id, Service.panel_username == username)
    )
    if used is not None:
        return False
    inserted = await session.execute(
        insert(PanelUsernameClaim)
        .values(panel_id=panel_id, username=username, order_id=order_id, created_at=utcnow())
        .on_conflict_do_nothing(index_elements=["panel_id", "username"])
        .returning(PanelUsernameClaim.id)
    )
    if inserted.first() is not None:
        return True
    holder = await session.scalar(
        select(PanelUsernameClaim.order_id).where(
            PanelUsernameClaim.panel_id == panel_id, PanelUsernameClaim.username == username
        )
    )
    return holder == order_id


def alternative(name: str) -> str:
    """The same name with a fresh random tail, still panel-safe and within the length limit."""
    return f"{name[: MAX_LEN - 7].rstrip('_')}_{secrets.token_hex(3)}"


async def release(session: AsyncSession, order_id: int) -> None:
    """Free names of an order that provably left nothing on a panel (refunded before creating)."""
    await session.execute(delete(PanelUsernameClaim).where(PanelUsernameClaim.order_id == order_id))
