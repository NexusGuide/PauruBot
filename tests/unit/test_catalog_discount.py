"""ClockBot parity: a time-limited discount on the whole catalog (or some categories) for
customers; off by default, and resellers keep their own prices."""

from __future__ import annotations

from datetime import timedelta

import pytest

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Category, ResellerProfile
from tests import factories


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop)
    category = Category(shop_id=shop.id, title="ویژه")
    session.add(category)
    await session.flush()
    plan = await factories.plan(session, shop, panel, price=100_000)
    other = await factories.plan(session, shop, panel, price=100_000)
    other.category_id = category.id
    customer = await factories.customer(session, shop)
    await session.commit()
    return {"shop": shop, "plan": plan, "other": other, "category": category, "customer": customer}


async def _price(session, env, plan):  # type: ignore[no-untyped-def]
    return (await pricing.plan_quote(session, env["customer"], plan.id, plan.price)).amount


async def test_off_by_default(session, env) -> None:  # type: ignore[no-untyped-def]
    assert await _price(session, env, env["plan"]) == 100_000


async def test_on_until_it_ends(session, env) -> None:  # type: ignore[no-untyped-def]
    until = (utcnow() + timedelta(hours=2)).isoformat()
    await shop_settings.set_many(
        session, env["shop"].id, {"catalog_discount_percent": 20, "catalog_discount_until": until}
    )
    assert await _price(session, env, env["plan"]) == 80_000
    await shop_settings.set_many(
        session, env["shop"].id, {"catalog_discount_until": (utcnow() - timedelta(minutes=1)).isoformat()}
    )
    assert await _price(session, env, env["plan"]) == 100_000


async def test_only_some_categories_and_never_resellers(session, env) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {"catalog_discount_percent": 10, "catalog_discount_category_ids": [env["category"].id]},
    )
    assert await _price(session, env, env["plan"]) == 100_000
    assert await _price(session, env, env["other"]) == 90_000
    session.add(ResellerProfile(shop_id=env["shop"].id, customer_id=env["customer"].id))
    await session.flush()
    await session.refresh(env["customer"], ["reseller_profile"])
    assert await _price(session, env, env["other"]) == 100_000  # its own reseller pricing applies
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, env["shop"].id, {"catalog_discount_percent": 95})
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, env["shop"].id, {"catalog_discount_until": "tomorrow"})
