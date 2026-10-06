from __future__ import annotations

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.audit.log import Actor, record
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, Service, ServiceStatus


async def upsert_from_telegram(
    session: AsyncSession,
    shop_id: int,
    telegram_id: int,
    *,
    username: str | None,
    first_name: str | None,
    locale: str | None = None,
) -> tuple[Customer, bool]:
    """Race-safe get-or-create: concurrent first messages from one user create a single row."""
    stmt = (
        insert(Customer)
        .values(
            shop_id=shop_id,
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            locale=locale,
            tags=[],
            last_seen_at=utcnow(),
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["shop_id", "telegram_id"])
        .returning(Customer.id)
    )
    inserted_id = await session.scalar(stmt)
    customer = await session.scalar(
        select(Customer)
        .where(Customer.shop_id == shop_id, Customer.telegram_id == telegram_id)
        .execution_options(populate_existing=True)
    )
    assert customer is not None
    if inserted_id is None:
        customer.username = username
        customer.first_name = first_name
        customer.last_seen_at = utcnow()
        customer.blocked_bot_at = None
    await session.flush()
    return customer, inserted_id is not None


async def get(session: AsyncSession, shop_id: int, customer_id: int) -> Customer | None:
    customer = await session.get(Customer, customer_id)
    if customer is None or customer.shop_id != shop_id:
        return None
    return customer


async def by_telegram(session: AsyncSession, shop_id: int, telegram_id: int) -> Customer | None:
    return await session.scalar(
        select(Customer).where(Customer.shop_id == shop_id, Customer.telegram_id == telegram_id)
    )


async def set_referrer(
    session: AsyncSession, customer: Customer, referrer_telegram_id: int
) -> Customer | None:
    if customer.referrer_id is not None or referrer_telegram_id == customer.telegram_id:
        return None
    referrer = await by_telegram(session, customer.shop_id, referrer_telegram_id)
    if referrer is None or referrer.id == customer.id:
        return None
    customer.referrer_id = referrer.id
    await session.flush()
    return referrer


async def set_ban(
    session: AsyncSession, customer: Customer, banned: bool, reason: str | None, actor: Actor
) -> None:
    before = {"is_banned": customer.is_banned, "ban_reason": customer.ban_reason}
    customer.is_banned = banned
    customer.ban_reason = reason if banned else None
    await record(
        session,
        shop_id=customer.shop_id,
        actor=actor,
        action="customer.ban" if banned else "customer.unban",
        entity="customer",
        entity_id=customer.id,
        before=before,
        after={"is_banned": customer.is_banned, "ban_reason": customer.ban_reason},
    )
    if before["is_banned"] != banned:
        await sync_services_with_ban(session, customer, banned)


async def sync_services_with_ban(session: AsyncSession, customer: Customer, banned: bool) -> int:
    """Disable a banned customer's services and re-enable only those a ban disabled (SVC-06)."""
    from paurubot.core import settings as shop_settings
    from paurubot.worker import queue

    if not await shop_settings.get(session, customer.shop_id, "ban_disables_services"):
        return 0
    stmt = select(Service.id).where(Service.customer_id == customer.id)
    if banned:
        stmt = stmt.where(
            Service.status.in_([ServiceStatus.ACTIVE, ServiceStatus.ON_HOLD, ServiceStatus.LIMITED])
        )
    else:
        stmt = stmt.where(Service.status == ServiceStatus.DISABLED, Service.disabled_reason == "ban")
    ids = list(await session.scalars(stmt))
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    for service_id in ids:
        await queue.enqueue(
            session,
            "bulk",
            "service",
            {"service_id": service_id, "action": "disable" if banned else "enable", "reason": "ban"},
            shop_id=customer.shop_id,
            dedupe_key=f"ban:{service_id}:{int(banned)}:{stamp}",
        )
    return len(ids)


async def search(
    session: AsyncSession, shop_id: int, query: str, *, limit: int = 25, offset: int = 0
) -> tuple[list[Customer], int]:
    stmt = select(Customer).where(Customer.shop_id == shop_id)
    query = query.strip().lstrip("@")
    if query:
        conditions: list[ColumnElement[bool]] = [
            Customer.username.ilike(f"%{query}%"),
            Customer.first_name.ilike(f"%{query}%"),
            Customer.phone.ilike(f"%{query}%"),
        ]
        if query.isdigit():
            conditions.append(Customer.telegram_id == int(query))
            conditions.append(Customer.id == int(query))
        stmt = stmt.where(or_(*conditions))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = await session.scalars(stmt.order_by(Customer.id.desc()).limit(limit).offset(offset))
    return list(rows), int(total or 0)


async def referral_count(session: AsyncSession, customer_id: int) -> int:
    return int(
        await session.scalar(select(func.count(Customer.id)).where(Customer.referrer_id == customer_id)) or 0
    )
