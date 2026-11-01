"""Changing a service's location (SVC-04): data and end date carry over, the link stays, the old account goes."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from paurubot.core import orders
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, Location, OrderKind, OrderStatus, PanelKind, ServiceStatus
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_service_lifecycle import _buy, _service, drain


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def two(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    de = Location(shop_id=shop.id, title="Germany", flag="🇩🇪")
    nl = Location(shop_id=shop.id, title="Netherlands")
    session.add_all([de, nl])
    await session.flush()
    old = await factories.panel(session, shop, PanelKind.MARZBAN, inbounds=["vless:VLESS TCP"])
    new = await factories.panel(session, shop, PanelKind.PASARGUARD, groups=[1])
    old.location_id, new.location_id = de.id, nl.id
    fakes = {old.id: FakeTokenPanel("marzban"), new.id: FakeTokenPanel("pasarguard")}
    for panel_id, fake in fakes.items():
        registry.test_transports[panel_id] = transport(fake)
    plan = await factories.plan(session, shop, old, price=100_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, 1_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    yield {"shop": shop, "old": old, "new": new, "nl": nl, "plan": plan, "cust": cust, "fakes": fakes}
    registry.test_transports.clear()
    registry._cache.clear()


async def test_move_keeps_remaining_data_end_date_and_link(session, two) -> None:  # type: ignore[no-untyped-def]
    cust, old, new = two["cust"], two["old"], two["new"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    old_fake = two["fakes"][old.id]
    old_fake.users[service.panel_username]["used_traffic"] = 20 * GB
    token, expire, old_username = service.sub_token, service.expire_at, service.panel_username

    targets = await orders.move_targets(session, service)
    assert [(loc.title, panel.id) for loc, panel in targets] == [("Netherlands", new.id)]
    await shop_settings.set_many(session, two["shop"].id, {"move_price": 15_000})
    before = await wallet_balance(session, two["shop"].id, cust.id)
    order = await _buy(session, cust, OrderKind.MOVE, service=service, move_to=new)
    assert order.amount == 15_000
    moved = await _service(session, order)
    await drain(session, "provision")  # removes the old account

    assert moved.id == service.id and moved.panel_id == new.id
    assert moved.sub_token == token and moved.expire_at == expire
    assert moved.data_limit == 30 * GB and moved.used_bytes == 0
    assert old_username not in old_fake.users
    assert moved.panel_username in two["fakes"][new.id].users
    assert await wallet_balance(session, two["shop"].id, cust.id) == before - 15_000


async def test_move_rules(session, two) -> None:  # type: ignore[no-untyped-def]
    cust, new, old = two["cust"], two["new"], two["old"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    with pytest.raises(orders.OrderError, match="move_unavailable"):
        await orders.draft(session, cust, OrderKind.MOVE, service=service, move_to=old)
    await shop_settings.set_many(session, two["shop"].id, {"move_enabled": False})
    with pytest.raises(orders.OrderError, match="move_unavailable"):
        await orders.draft(session, cust, OrderKind.MOVE, service=service, move_to=new)
    await shop_settings.set_many(session, two["shop"].id, {"move_enabled": True})
    service.status = ServiceStatus.EXPIRED
    with pytest.raises(orders.OrderError, match="move_not_now"):
        await orders.draft(session, cust, OrderKind.MOVE, service=service, move_to=new)


async def test_a_reused_account_is_never_removed(session, two) -> None:  # type: ignore[no-untyped-def]
    from paurubot.provisioning import lifecycle

    cust, old = two["cust"], two["old"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    await lifecycle.remove_panel_user(session, old.id, service.panel_username, {})
    assert service.panel_username in two["fakes"][old.id].users  # still tracked, so kept


async def test_failed_move_refunds(session, two) -> None:  # type: ignore[no-untyped-def]
    cust, new = two["cust"], two["new"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    await shop_settings.set_many(session, two["shop"].id, {"move_price": 10_000})
    order = await _buy(session, cust, OrderKind.MOVE, service=service, move_to=new)
    new.is_active = False
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    await session.refresh(service)
    assert order.status in {OrderStatus.FAILED, OrderStatus.REFUNDED}
    assert service.panel_id == two["old"].id


async def test_staff_move_one_and_many(session, two) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from paurubot.api.app import create_app
    from paurubot.auth.staff import create_staff
    from paurubot.db.models import Service, StaffRole

    cust, new = two["cust"], two["new"]
    first = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    second = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    await create_staff(
        session, two["shop"].id, role=StaffRole.OWNER, username="mover", password="a-very-strong-pass"
    )
    await session.commit()
    transport_ = httpx.ASGITransport(app=create_app(), client=("10.2.3.4", 4000))
    async with httpx.AsyncClient(transport=transport_, base_url="http://testserver") as client:
        login = await client.post(
            "/api/auth/login", json={"username": "mover", "password": "a-very-strong-pass"}
        )
        csrf = {"x-csrf-token": login.json()["csrf_token"]}
        same = await client.post(
            f"/api/admin/services/{first.id}/move", json={"panel_id": two["old"].id}, headers=csrf
        )
        assert same.status_code == 409 and same.json()["error"] == "move_unavailable"
        one = await client.post(
            f"/api/admin/services/{first.id}/move", json={"panel_id": new.id}, headers=csrf
        )
        assert one.status_code == 200
        await drain(session, "provision")
        bulk = await client.post(
            "/api/admin/services/bulk",
            json={"action": "move", "ids": [first.id, second.id], "panel_id": new.id},
            headers=csrf,
        )
        assert bulk.json() == {"queued": 1, "skipped": 1}  # the first one is already there
        await drain(session, "provision")
    for service in (first, second):
        row = await session.get(Service, service.id, populate_existing=True)
        assert row is not None and row.panel_id == new.id


async def test_move_never_lands_on_someone_elses_account(session, two, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """SEC-F03: the target panel already has an account under the generated name."""
    from paurubot.core import usernames

    cust, new = two["cust"], two["new"]
    service = await _service(session, await _buy(session, cust, OrderKind.PURCHASE, plan=two["plan"]))
    new_fake = two["fakes"][new.id]
    new_fake.users["moved_1"] = {
        "id": 77,
        "username": "moved_1",
        "status": "active",
        "used_traffic": 5,
        "data_limit": 1,
        "expire": 0,
        "sub": "theirs",
        "request": {},
    }
    foreign = dict(new_fake.users["moved_1"])

    async def same_name(*_args, **_kwargs) -> str:  # type: ignore[no-untyped-def]
        return "moved_1"

    monkeypatch.setattr(usernames, "generate", same_name)
    order = await _buy(session, cust, OrderKind.MOVE, service=service, move_to=new)
    moved = await _service(session, order)
    assert moved.panel_id == new.id and moved.panel_username.startswith("moved_1_")
    assert new_fake.users["moved_1"] == foreign
