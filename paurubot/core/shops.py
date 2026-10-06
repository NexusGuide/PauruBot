from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.db.models import Shop, Staff, StaffRole


async def root_shop(session: AsyncSession) -> Shop | None:
    return await session.scalar(select(Shop).where(Shop.parent_id.is_(None)).order_by(Shop.id).limit(1))


async def require_root_shop(session: AsyncSession) -> Shop:
    shop = await root_shop(session)
    if shop is None:
        raise LookupError("setup has not been completed")
    return shop


async def is_set_up(session: AsyncSession) -> bool:
    count = await session.scalar(select(func.count(Staff.id)).where(Staff.role == StaffRole.OWNER))
    return bool(count)


async def create_shop(
    session: AsyncSession,
    title: str,
    *,
    parent_id: int | None = None,
    currency: str = "IRT",
    locale: str = "fa",
) -> Shop:
    shop = Shop(title=title.strip()[:96], parent_id=parent_id, currency=currency, default_locale=locale)
    session.add(shop)
    await session.flush()
    from paurubot.ledger.accounts import ensure_system_accounts

    await ensure_system_accounts(session, shop.id)
    return shop
