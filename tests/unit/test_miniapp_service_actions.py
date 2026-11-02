"""F-03: the Mini App can change a service's location and delete it, as the bot can."""

from __future__ import annotations

from datetime import timedelta

from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Location, Order, OrderKind, PanelKind, Service, ServiceStatus
from paurubot.db.session import session_scope
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit import test_webapp_api
from tests.unit.test_webapp_api import h

app_env = test_webapp_api.app_env


async def _service(session, app_env, uid: int):  # type: ignore[no-untyped-def]
    client, shop, panel = app_env["client"], app_env["shop"], app_env["panel"]
    me = (await client.get("/api/webapp/me", headers=h(uid))).json()
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    registry._cache.clear()
    expire = utcnow().replace(microsecond=0) + timedelta(days=15)
    plan = app_env["plan"]
    service = Service(
        shop_id=shop.id,
        customer_id=me["id"],
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username=f"u{uid}",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * GB,
        used_bytes=2 * GB,
        expire_at=expire,
    )
    session.add(service)
    fake.users[f"u{uid}"] = {
        "username": f"u{uid}",
        "status": "active",
        "used_traffic": 2 * GB,
        "data_limit": 10 * GB,
        "expire": int(expire.timestamp()),
        "sub": "s",
    }
    await session.commit()
    return service, me, fake


async def test_actions_follow_the_shop_settings(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    service, _, _ = await _service(session, app_env, 92001)
    view = (await client.get(f"/api/webapp/services/{service.id}", headers=h(92001))).json()["service"]
    assert view["actions"] == ["move"]  # moving is on by default, deleting is not
    async with session_scope() as s:
        await shop_settings.set_many(s, shop.id, {"self_delete_enabled": True, "move_enabled": False})
    view = (await client.get(f"/api/webapp/services/{service.id}", headers=h(92001))).json()["service"]
    assert view["actions"] == ["cancel"]
    # Another customer's service is not found, for any action.
    for path in ("move", "cancel"):
        assert (
            await client.get(f"/api/webapp/services/{service.id}/{path}", headers=h(92002))
        ).status_code == 404
    assert (
        await client.post(f"/api/webapp/services/{service.id}/cancel", headers=h(92002))
    ).status_code == 404
    registry.test_transports.clear()


async def test_move_lists_destinations_and_orders_one(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    service, me, _ = await _service(session, app_env, 92011)
    here, there = Location(shop_id=shop.id, title="Here"), Location(shop_id=shop.id, title="There", flag="🇳🇱")
    session.add_all([here, there])
    await session.flush()
    app_env["panel"].location_id = here.id
    other = await factories.panel(session, shop, PanelKind.MARZBAN)
    other.location_id = there.id
    await session.commit()
    async with session_scope() as s:
        await shop_settings.set_many(s, shop.id, {"move_enabled": False})
    assert (await client.get(f"/api/webapp/services/{service.id}/move", headers=h(92011))).status_code == 409
    async with session_scope() as s:
        await shop_settings.set_many(s, shop.id, {"move_enabled": True, "move_price": 5_000})
    options = (await client.get(f"/api/webapp/services/{service.id}/move", headers=h(92011))).json()
    assert options == {"price": 5_000, "targets": [{"location_id": there.id, "title": "There", "flag": "🇳🇱"}]}
    bad = await client.post(
        "/api/webapp/orders",
        headers=h(92011),
        json={"kind": "move", "service_id": service.id, "location_id": here.id},  # where it already is
    )
    assert bad.status_code == 400
    ok = await client.post(
        "/api/webapp/orders",
        headers=h(92011),
        json={"kind": "move", "service_id": service.id, "location_id": there.id},
    )
    assert ok.status_code == 200, ok.text
    order = await session.get(Order, ok.json()["order"]["id"])
    assert order is not None and order.kind == OrderKind.MOVE and order.params["move_to"] == other.id
    assert order.amount == 5_000 and order.customer_id == me["id"]
    registry.test_transports.clear()


async def test_cancel_quotes_then_deletes_and_refunds(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop = app_env["client"], app_env["shop"]
    service, me, fake = await _service(session, app_env, 92021)
    paid = Order(
        shop_id=shop.id,
        customer_id=me["id"],
        kind=OrderKind.PURCHASE,
        status="completed",
        plan_id=app_env["plan"].id,
        service_id=service.id,
        amount=100_000,
        completed_at=utcnow(),
    )
    session.add(paid)
    await session.commit()
    assert (
        await client.get(f"/api/webapp/services/{service.id}/cancel", headers=h(92021))
    ).status_code == 409
    async with session_scope() as s:
        await shop_settings.set_many(
            s, shop.id, {"self_delete_enabled": True, "self_delete_refund_percent": 100}
        )
    quote = (await client.get(f"/api/webapp/services/{service.id}/cancel", headers=h(92021))).json()
    assert quote["refund"] > 0
    done = await client.post(f"/api/webapp/services/{service.id}/cancel", headers=h(92021))
    assert done.status_code == 200, done.text
    assert done.json()["refund"] == quote["refund"]
    assert "u92021" not in fake.users
    assert await wallet_balance(session, shop.id, me["id"]) == quote["refund"]
    again = await client.post(f"/api/webapp/services/{service.id}/cancel", headers=h(92021))
    assert again.status_code == 404  # already deleted
    registry.test_transports.clear()
