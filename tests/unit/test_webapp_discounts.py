"""F-33: the Mini App shows the customer's own discounts, and which fit the order at hand.

Before, a customer found out whether a code applied only by typing it at checkout, and the Mini
App showed neither the codes made for them nor how far the next loyalty tier was.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from paurubot.core import pricing
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Customer, DiscountCode, DiscountKind
from tests import factories
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env


async def _me(session, shop, client, uid: int) -> Customer:  # type: ignore[no-untyped-def]
    await client.get("/api/webapp/me", headers=h(uid))
    row = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == uid)
    )
    assert row is not None
    return row


def _code(shop, code: str, **kw) -> DiscountCode:  # type: ignore[no-untyped-def]
    values = {"kind": DiscountKind.PERCENT, "value": 10, "per_customer_limit": 1, **kw}
    return DiscountCode(shop_id=shop.id, code=code, **values)


async def test_own_codes_are_listed_and_checked_against_the_order(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan = app_env["client"], app_env["shop"], app_env["plan"]  # plan costs 90,000
    me = await _me(session, shop, client, 96001)
    other = await _me(session, shop, client, 96002)
    other_plan = await factories.plan(session, shop, app_env["panel"], price=50_000)
    session.add_all(
        [
            _code(shop, "MINE10", customer_id=me.id),
            _code(shop, "FIXED20K", customer_id=me.id, kind=DiscountKind.FIXED, value=20_000),
            _code(shop, "BIGONLY", customer_id=me.id, min_amount=500_000),
            _code(shop, "OTHERPLAN", customer_id=me.id, plan_ids=[other_plan.id]),
            _code(shop, "OLD", customer_id=me.id, expires_at=utcnow() - timedelta(days=1)),
            _code(shop, "THEIRS", customer_id=other.id),
            _code(shop, "PUBLIC50", value=50),  # a public code is never revealed
        ]
    )
    await session.commit()

    plain = (await client.get("/api/webapp/discounts", headers=h(96001))).json()
    listed = {c["code"] for c in plain["codes"]}
    assert listed == {"MINE10", "FIXED20K", "BIGONLY", "OTHERPLAN"}
    assert all(c["fits"] is None and c["saving"] is None for c in plain["codes"])  # no order, no verdict

    order = (
        await client.post(
            "/api/webapp/orders", headers=h(96001), json={"kind": "purchase", "plan_id": plan.id}
        )
    ).json()["order"]
    checked = (await client.get(f"/api/webapp/discounts?order_id={order['id']}", headers=h(96001))).json()
    by_code = {c["code"]: c for c in checked["codes"]}
    assert [c["code"] for c in checked["codes"]][:2] == ["FIXED20K", "MINE10"]  # best saving first
    assert by_code["FIXED20K"]["saving"] == 20_000 and by_code["MINE10"]["saving"] == 9_000
    assert by_code["BIGONLY"] == {**by_code["BIGONLY"], "fits": False, "reason": "discount_min_amount"}
    assert by_code["OTHERPLAN"]["fits"] is False and by_code["OTHERPLAN"]["reason"] == "discount_wrong_plan"
    # The saving is what checkout really gives.
    code = await session.scalar(select(DiscountCode).where(DiscountCode.code == "MINE10"))
    assert pricing.code_discount_amount(code, order["amount"]) == by_code["MINE10"]["saving"]

    # Someone else's order is not found; a paid or canceled one gets no verdicts.
    assert (
        await client.get(f"/api/webapp/discounts?order_id={order['id']}", headers=h(96002))
    ).status_code == 404
    await client.post(f"/api/webapp/orders/{order['id']}/cancel", headers=h(96001))
    after = (await client.get(f"/api/webapp/discounts?order_id={order['id']}", headers=h(96001))).json()
    assert all(c["fits"] is None for c in after["codes"])


async def test_tier_progress_and_a_sticky_code(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    me = await _me(session, shop, client, 96011)
    tiers = [
        {"title": "Silver", "min_spent": 100_000, "percent": 5},
        {"title": "Gold", "min_spent": 500_000, "percent": 10},
    ]
    await shop_settings.set_many(session, shop.id, {"loyalty_enabled": True, "loyalty_tiers": tiers})
    shop_settings.invalidate(shop.id)
    me.total_spent, me.tier_percent = 150_000, 5
    sticky = _code(shop, "LOYAL5", value=5, sticky=True, per_customer_limit=0)
    session.add(sticky)
    await session.flush()
    await pricing.attach_sticky(session, me, sticky)
    await session.commit()
    data = (await client.get("/api/webapp/discounts", headers=h(96011))).json()
    assert data["standing_percent"] == 5 and data["reseller"] is False
    assert data["tier"] == {"title": "Silver", "percent": 5}
    assert data["next_tier"] == {"title": "Gold", "percent": 10, "remaining": 350_000}
    assert [c["code"] for c in data["codes"]] == ["LOYAL5"] and data["codes"][0]["sticky"] is True


async def test_nothing_to_show_is_an_empty_answer(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    data = (await client.get("/api/webapp/discounts", headers=h(96021))).json()
    assert data == {"standing_percent": 0, "reseller": False, "tier": None, "next_tier": None, "codes": []}
    assert (await client.get("/api/webapp/discounts?order_id=0", headers=h(96021))).status_code == 422
