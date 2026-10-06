"""Small builders for test data."""

from __future__ import annotations

import itertools

from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import customers
from paurubot.core.shops import create_shop
from paurubot.db.models import Customer, Panel, PanelKind, Plan, Shop
from paurubot.security.secretbox import encrypt

_ids = itertools.count(1_000_000)


async def shop(session: AsyncSession, title: str = "Test shop") -> Shop:
    return await create_shop(session, title)


async def customer(
    session: AsyncSession, shop_obj: Shop, telegram_id: int | None = None, username: str | None = "tester"
) -> Customer:
    row, _ = await customers.upsert_from_telegram(
        session, shop_obj.id, telegram_id or next(_ids), username=username, first_name="Test"
    )
    return row


async def panel(
    session: AsyncSession, shop_obj: Shop, kind: PanelKind = PanelKind.MARZBAN, **options
) -> Panel:  # type: ignore[no-untyped-def]
    row = Panel(
        shop_id=shop_obj.id,
        name=f"{kind.value}-panel",
        kind=kind,
        base_url="https://panel.example.com",
        username="admin",
        password_enc=encrypt("panel", "panel-password"),
        options=options,
    )
    session.add(row)
    await session.flush()
    return row


async def plan(
    session: AsyncSession,
    shop_obj: Shop,
    panel_obj: Panel,
    *,
    price: int = 100_000,
    days: int = 30,
    data_gb: int = 50,
    title: str = "Monthly 50GB",
) -> Plan:
    row = Plan(
        shop_id=shop_obj.id, panel_id=panel_obj.id, title=title, price=price, days=days, data_gb=data_gb
    )
    session.add(row)
    await session.flush()
    return row
