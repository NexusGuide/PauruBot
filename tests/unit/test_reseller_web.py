"""Resellers on the web and in the Mini App (TEN-15): a sign-in made on approval, the selling desk
in the web panel, and the desk and customer name in the Mini App."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.api import webapp_auth
from paurubot.api.app import create_app
from paurubot.auth.staff import create_staff
from paurubot.core import resellers
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    ApplicationStatus,
    Job,
    JobStatus,
    Order,
    ResellerApplication,
    ResellerGroup,
    ResellerProfile,
    Service,
    ServiceStatus,
    Staff,
    StaffRole,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.panels import GB, registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.unit.test_resellers import drain
from tests.unit.test_webapp_api import h

PASSWORD = "a-very-strong-pass"


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Web resell")
    panel = await factories.panel(session, shop)
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    group = ResellerGroup(shop_id=shop.id, title="Gold", discount_percent=20)
    session.add(group)
    await create_staff(session, shop.id, role=StaffRole.OWNER, username="web_owner", password=PASSWORD)
    await session.commit()

    async def our_shop(_session):  # type: ignore[no-untyped-def]
        return shop

    monkeypatch.setattr(webapp_auth, "root_shop", our_shop)
    app = httpx.ASGITransport(app=create_app(), client=("10.9.8.7", 4000))
    async with httpx.AsyncClient(transport=app, base_url="http://testserver") as client:
        yield {"shop": shop, "panel": panel, "plan": plan, "group": group, "client": client, "fake": fake}
    registry.test_transports.clear()
    registry._cache.clear()


async def _login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    client.cookies.clear()
    resp = await client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"x-csrf-token": resp.json()["csrf_token"]}


async def test_approval_gives_a_telegram_code_sign_in(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, client, group = env["shop"], env["client"], env["group"]
    cust = await factories.customer(session, shop)
    application = ResellerApplication(shop_id=shop.id, customer_id=cust.id, text="please")
    session.add(application)
    await session.commit()
    csrf = await _login(client, "web_owner")
    resp = await client.post(
        f"/api/admin/reseller-applications/{application.id}",
        json={"approve": True, "group_id": group.id},
        headers=csrf,
    )
    assert resp.status_code == 200, resp.text
    staff = await session.scalar(select(Staff).where(Staff.telegram_id == cust.telegram_id))
    assert staff is not None and staff.role == StaffRole.RESELLER
    assert staff.username == f"r{cust.telegram_id}" and staff.password_hash is None
    profile = await session.get(ResellerProfile, staff.reseller_profile_id)
    assert profile is not None and profile.customer_id == cust.id
    note = await session.scalar(select(Job).where(Job.kind == "reseller_decision"))
    assert note is not None and note.payload["web_username"] == staff.username
    await session.refresh(application)
    assert application.status == ApplicationStatus.APPROVED
    # Asking again (the bot's "Web panel" button) returns the same sign-in.
    assert (await resellers.web_login(session, profile)).id == staff.id


async def test_no_sign_in_when_switched_off(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, group = env["shop"], env["group"]
    await shop_settings.set_many(session, shop.id, {"reseller_web_access": False})
    cust = await factories.customer(session, shop)
    profile = ResellerProfile(shop_id=shop.id, customer_id=cust.id, group_id=group.id)
    session.add(profile)
    await session.flush()
    assert await resellers.web_login(session, profile) is None


async def _reseller_staff(session, env, balance):  # type: ignore[no-untyped-def]
    shop, group = env["shop"], env["group"]
    cust = await factories.customer(session, shop)
    await wallet.topup(session, shop.id, cust.id, balance, provider="card", payment_id=cust.id)
    profile = ResellerProfile(
        shop_id=shop.id,
        customer_id=cust.id,
        group_id=group.id,
        permissions=["services.create", "services.read"],
    )
    session.add(profile)
    await session.flush()
    await create_staff(
        session,
        shop.id,
        role=StaffRole.RESELLER,
        username=f"rs_{cust.id}",
        password=PASSWORD,
        reseller_profile_id=profile.id,
    )
    await session.commit()
    return cust


async def test_web_desk_sells_and_renews_from_the_wallet(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client, fake = env["shop"], env["plan"], env["client"], env["fake"]
    cust = await _reseller_staff(session, env, 230_000)
    csrf = await _login(client, f"rs_{cust.id}")
    desk = (await client.get("/api/admin/desk", headers=csrf)).json()
    assert desk["balance"] == 230_000
    assert [(p["id"], p["price"]) for p in desk["plans"]] == [(plan.id, 80_000)]
    sold = await client.post(
        "/api/admin/desk/sell", json={"plan_id": plan.id, "label": "Ali phone"}, headers=csrf
    )
    assert sold.status_code == 200, sold.text
    assert sold.json()["balance"] == 150_000
    await drain(session, "provision")
    order = await session.get(Order, sold.json()["order_id"])
    assert order is not None and order.status == "completed"
    service = await session.get(Service, order.service_id)
    assert service is not None and service.label == "Ali phone" and service.customer_id == cust.id
    # It runs out soon: listed with its renewal price, renewed in one go.
    service.expire_at = utcnow() + timedelta(days=1)
    fake.users[service.panel_username]["expire"] = int(service.expire_at.timestamp())
    await session.commit()
    rows = (await client.get("/api/admin/desk/expiring?days=3", headers=csrf)).json()["items"]
    assert [(r["id"], r["renew_price"], r["label"]) for r in rows] == [(service.id, 80_000, "Ali phone")]
    renewed = await client.post("/api/admin/desk/renew", json={"service_ids": [service.id]}, headers=csrf)
    assert renewed.json()["renewed"] == [service.id] and renewed.json()["balance"] == 70_000
    # Too little left for another plan: the sale is refused and nothing is charged.
    broke = await client.post("/api/admin/desk/sell", json={"plan_id": plan.id}, headers=csrf)
    assert broke.status_code == 400 and broke.json()["error"] == "insufficient_balance"
    assert await wallet_balance(session, shop.id, cust.id) == 70_000


async def test_desk_is_the_resellers_own(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, client, panel, plan = env["shop"], env["client"], env["panel"], env["plan"]
    cust = await _reseller_staff(session, env, 100_000)
    stranger = await factories.customer(session, shop)
    theirs = Service(
        shop_id=shop.id,
        customer_id=stranger.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="u_stranger",
        status=ServiceStatus.ACTIVE,
        data_limit=50 * GB,
        expire_at=utcnow() + timedelta(days=1),
    )
    session.add(theirs)
    await session.commit()
    csrf = await _login(client, f"rs_{cust.id}")
    resp = await client.post("/api/admin/desk/renew", json={"service_ids": [theirs.id]}, headers=csrf)
    assert resp.json()["renewed"] == [] and resp.json()["failed"] == []
    assert await wallet_balance(session, shop.id, cust.id) == 100_000
    owner = await _login(client, "web_owner")
    assert (await client.get("/api/admin/desk", headers=owner)).status_code == 404


async def test_mini_app_desk_and_customer_name(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, client, group = env["shop"], env["plan"], env["client"], env["group"]
    uid = 9301
    assert (await client.get("/api/webapp/reseller/desk", headers=h(uid))).status_code == 403
    me = (await client.get("/api/webapp/me", headers=h(uid))).json()
    session.add(ResellerProfile(shop_id=shop.id, customer_id=me["id"], group_id=group.id))
    await wallet.topup(session, shop.id, me["id"], 300_000, provider="card", payment_id=uid)
    await session.commit()
    created = await client.post(
        "/api/webapp/orders", headers=h(uid), json={"kind": "purchase", "plan_id": plan.id, "label": "Reza"}
    )
    assert created.status_code == 200, created.text
    assert (
        created.json()["order"]["params"]["label"] == "Reza" and created.json()["order"]["amount"] == 80_000
    )
    order_id = created.json()["order"]["id"]
    await client.post(f"/api/webapp/orders/{order_id}/pay", headers=h(uid), json={"method": "wallet"})
    await drain(session, "provision")
    order = await session.get(Order, order_id)
    assert order is not None and order.service_id is not None
    service = await session.get(Service, order.service_id)
    assert service is not None and service.label == "Reza"
    service.expire_at = utcnow() + timedelta(days=2)
    env["fake"].users[service.panel_username]["expire"] = int(service.expire_at.timestamp())
    await session.commit()
    desk = (await client.get("/api/webapp/reseller/desk?days=3", headers=h(uid))).json()
    assert desk["figures"]["active"] == 1 and desk["figures"]["expiring"] == 1
    assert [(r["id"], r["renew_price"]) for r in desk["expiring"]] == [(service.id, 80_000)]
    renewed = await client.post(
        "/api/webapp/reseller/renew", headers=h(uid), json={"service_ids": [service.id]}
    )
    assert renewed.json()["renewed"] == [service.id] and renewed.json()["balance"] == 140_000
