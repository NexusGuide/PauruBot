"""Two adds at the same moment can't both take the last slot (audit BE-01).

Forced channels and link domains counted the shop's rows and inserted in separate steps, with a
Telegram lookup in between for channels. Two requests at once both saw room and both saved, so a
shop could pass its limit. The shop row is now locked for the count and the insert.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from paurubot.core import domains, forced_join
from paurubot.db.models import ForcedChannel, ShopDomain
from tests import factories


async def test_concurrent_channel_adds_respect_the_limit(engine, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        shop = await factories.shop(s, "Channel race")
        for n in range(forced_join.MAX_CHANNELS - 1):
            s.add(ForcedChannel(shop_id=shop.id, chat_id=f"-100800{n}", title=f"c{n}", url="https://t.me/c"))
        await s.commit()

    async def slow_resolve(shop_id: int, raw: str) -> forced_join.ResolvedChannel:
        await asyncio.sleep(0.05)  # Telegram answering while the other request runs
        number = int(raw.lstrip("@")[-1])
        return forced_join.ResolvedChannel(-1009000 - number, raw, f"https://t.me/{raw[1:]}", True)

    monkeypatch.setattr(forced_join, "resolve", slow_resolve)

    async def add(name: str) -> bool:
        async with maker() as s2:
            try:
                await forced_join.add(s2, shop.id, name)
                await s2.commit()
                return True
            except forced_join.ForcedJoinError:
                await s2.rollback()
                return False

    results = await asyncio.gather(add("@race_one1"), add("@race_two2"))
    assert sorted(results) == [False, True]
    async with maker() as s:
        count = await s.scalar(select(func.count(ForcedChannel.id)).where(ForcedChannel.shop_id == shop.id))
    assert count == forced_join.MAX_CHANNELS


async def test_adding_a_domain_locks_the_shop_first(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await session.commit()
    locked: list[int] = []
    real = domains.lock_shop

    async def spy(s, shop_id: int) -> None:  # type: ignore[no-untyped-def]
        locked.append(shop_id)
        await real(s, shop_id)

    monkeypatch.setattr(domains, "lock_shop", spy)
    await domains.add(session, shop.id, "links.example.org")
    assert locked == [shop.id]
    assert await session.scalar(select(func.count(ShopDomain.id))) == 1
