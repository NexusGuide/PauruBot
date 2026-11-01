"""SEC-F11: the Mini App asks for phone and terms exactly as the bot does, and lets the customer fix it.

Before: the Mini App enforced only ban, maintenance and forced channels. A customer the bot would
stop for a missing phone number or unaccepted terms could still buy, top up or transfer there.
"""

from __future__ import annotations

from sqlalchemy import select

from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Staff, StaffRole
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env  # the shared Mini App client fixture


async def _require(session, shop, **values) -> None:  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, shop.id, values)
    shop_settings.invalidate(shop.id)
    await session.commit()


async def _customer(session, shop, uid: int) -> Customer:  # type: ignore[no-untyped-def]
    row = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == uid)
    )
    assert row is not None
    await session.refresh(row)
    return row


async def test_purchase_routes_answer_phone_required_until_the_phone_is_known(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan = app_env["client"], app_env["shop"], app_env["plan"]
    await _require(session, shop, require_phone=True)
    for method, path, body in (
        ("get", "/api/webapp/me", None),
        ("post", "/api/webapp/orders", {"kind": "purchase", "plan_id": plan.id}),
        ("post", "/api/webapp/wallet/topup", {"amount": 100_000, "method": "card"}),
    ):
        response = await getattr(client, method)(path, headers=h(9101), **({"json": body} if body else {}))
        assert response.status_code == 403 and response.json()["error"] == "phone_required", path
    gate = (await client.get("/api/webapp/gate", headers=h(9101))).json()
    assert gate["requirement"] == "phone_required" and "terms" not in gate
    # The bot saves the shared contact; then the Mini App opens.
    customer = await _customer(session, shop, 9101)
    customer.phone = "989121234567"
    await session.commit()
    assert (await client.get("/api/webapp/gate", headers=h(9101))).json()["requirement"] is None
    assert (await client.get("/api/webapp/me", headers=h(9101))).status_code == 200


async def test_terms_are_accepted_in_the_app_for_the_version_shown(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    await _require(session, shop, terms_required=True, terms_version=3)
    assert (await client.get("/api/webapp/catalog", headers=h(9201))).json()["error"] == "terms_required"
    gate = (await client.get("/api/webapp/gate", headers=h(9201))).json()
    assert gate["requirement"] == "terms_required" and gate["terms"]["version"] == 3
    assert "<b>" not in gate["terms"]["text"] and gate["terms"]["text"]  # plain text, not Telegram HTML
    stale = await client.post("/api/webapp/gate/terms", headers=h(9201), json={"version": 2})
    assert stale.status_code == 409 and stale.json()["error"] == "terms_changed"
    accepted = await client.post("/api/webapp/gate/terms", headers=h(9201), json={"version": 3})
    assert accepted.status_code == 200 and accepted.json()["requirement"] is None
    assert (await _customer(session, shop, 9201)).terms_accepted_version == 3
    assert (await client.get("/api/webapp/catalog", headers=h(9201))).status_code == 200
    # A new version of the terms closes the app again, as the bot does.
    await _require(session, shop, terms_version=4)
    assert (await client.get("/api/webapp/me", headers=h(9201))).json()["error"] == "terms_required"


async def test_phone_comes_before_terms(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    await _require(session, shop, require_phone=True, terms_required=True)
    assert (await client.get("/api/webapp/gate", headers=h(9301))).json()["requirement"] == "phone_required"
    early = await client.post("/api/webapp/gate/terms", headers=h(9301), json={"version": 1})
    assert early.status_code == 409 and early.json()["error"] == "phone_required"


async def test_staff_are_exempt_like_in_the_bot_but_reseller_accounts_are_not(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    await _require(session, shop, require_phone=True)
    session.add_all(
        [
            Staff(
                shop_id=shop.id, telegram_id=9401, role=StaffRole.SUPPORT, username="sup9401", is_active=True
            ),
            Staff(
                shop_id=shop.id, telegram_id=9402, role=StaffRole.RESELLER, username="res9402", is_active=True
            ),
        ]
    )
    await session.commit()
    assert (await client.get("/api/webapp/me", headers=h(9401))).status_code == 200
    assert (await client.get("/api/webapp/me", headers=h(9402))).json()["error"] == "phone_required"


async def test_forced_channels_still_come_first(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import ForcedChannel

    client, shop = app_env["client"], app_env["shop"]
    await _require(session, shop, require_phone=True)
    session.add(
        ForcedChannel(shop_id=shop.id, chat_id="@gate_news", title="News", url="https://t.me/gate_news")
    )
    await session.commit()
    app_env["fake"].non_members.add(("@gate_news", 9501))
    response = await client.get("/api/webapp/gate", headers=h(9501))
    assert response.status_code == 403 and response.json()["error"] == "join_required"
