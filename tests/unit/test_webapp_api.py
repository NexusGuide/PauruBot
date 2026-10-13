from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlalchemy import select

from paurubot.api import webapp_auth
from paurubot.api.app import create_app
from paurubot.core import settings as shop_settings
from paurubot.db.models import Payment, PaymentStatus, Service, ServiceStatus
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.security.tokens import sign_init_data
from tests import factories
from tests.fakes.telegram import TOKEN, make_bot, png_bytes


def init_data(uid: int, *, age: int = 0) -> str:
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AA",
        "user": json.dumps({"id": uid, "first_name": "Sara", "language_code": "en"}),
    }
    return sign_init_data(fields, TOKEN)


@pytest.fixture
async def app_env(session, monkeypatch):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Mini shop")
    await shop_settings.set_many(session, shop.id, {"card_accounts": [{"number": "6037990000000000"}]})
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=90_000)
    await session.commit()

    async def our_shop(_session):  # type: ignore[no-untyped-def]
        return shop

    monkeypatch.setattr(webapp_auth, "root_shop", our_shop)
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        yield {"client": client, "shop": shop, "plan": plan, "panel": panel, "fake": fake}
    notify_telegram.set_bot(None)


def h(uid: int) -> dict[str, str]:
    return {"x-telegram-init-data": init_data(uid)}


async def test_rejects_missing_or_stale_init_data(app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    assert (await client.get("/api/webapp/me")).status_code == 401
    stale = {"x-telegram-init-data": init_data(1, age=10 * 86400)}
    assert (await client.get("/api/webapp/me", headers=stale)).status_code == 401


async def test_profile_catalog_and_wallet_purchase(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan = app_env["client"], app_env["shop"], app_env["plan"]
    me = (await client.get("/api/webapp/me", headers=h(8101))).json()
    assert me["balance"] == 0 and me["locale"] == "en" and "card" in me["payment_methods"]
    catalog = (await client.get("/api/webapp/catalog", headers=h(8101))).json()
    assert [p["id"] for p in catalog["plans"]] == [plan.id]
    created = await client.post(
        "/api/webapp/orders", headers=h(8101), json={"kind": "purchase", "plan_id": plan.id}
    )
    assert created.status_code == 200 and created.json()["due"] == 90_000
    order_id = created.json()["order"]["id"]
    broke = await client.post(
        f"/api/webapp/orders/{order_id}/pay", headers=h(8101), json={"method": "wallet"}
    )
    assert broke.status_code == 400 and broke.json()["error"] == "insufficient_balance"
    await wallet.topup(session, shop.id, me["id"], 100_000, provider="card", payment_id=81010)
    await session.commit()
    paid = await client.post(f"/api/webapp/orders/{order_id}/pay", headers=h(8101), json={"method": "wallet"})
    assert paid.json()["order"]["status"] == "paid"


async def test_card_payment_with_receipt_upload(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    started = await client.post(
        "/api/webapp/wallet/topup", headers=h(8201), json={"amount": 100_000, "method": "card"}
    )
    assert started.status_code == 200
    payment_id = started.json()["payment"]["payment_id"]
    assert started.json()["payment"]["accounts"][0]["number"] == "6037990000000000"
    upload = await client.post(
        f"/api/webapp/payments/{payment_id}/receipt",
        headers=h(8201),
        files={"file": ("r.png", png_bytes(), "image/png")},
    )
    assert upload.status_code == 200
    payment = await session.get(Payment, payment_id)
    await session.refresh(payment)
    assert payment.status == PaymentStatus.VERIFYING and payment.receipt_sha256
    other = await client.post(
        f"/api/webapp/payments/{payment_id}/receipt",
        headers=h(8202),
        files={"file": ("r.png", png_bytes(), "image/png")},
    )
    assert other.status_code == 404


async def test_customers_cannot_touch_each_others_services(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, panel = app_env["client"], app_env["shop"], app_env["panel"]
    await client.get("/api/webapp/me", headers=h(8301))
    owner = await factories.customer(session, shop, telegram_id=8399)
    service = Service(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=panel.id,
        panel_username="victim",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.commit()
    for method, path in (
        ("GET", f"/api/webapp/services/{service.id}"),
        ("POST", f"/api/webapp/services/{service.id}/revoke"),
        ("GET", f"/api/webapp/services/{service.id}/links"),
    ):
        resp = await client.request(method, path, headers=h(8301))
        assert resp.status_code == 404, path
    listed = (await client.get("/api/webapp/services", headers=h(8301))).json()
    assert listed["items"] == []
    renew = await client.post(
        "/api/webapp/orders",
        headers=h(8301),
        json={"kind": "renew", "plan_id": app_env["plan"].id, "service_id": service.id},
    )
    assert renew.status_code == 404
    assert await session.scalar(select(Service).where(Service.id == service.id)) is not None


async def test_staff_tools_follow_the_role(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.auth.staff import create_staff
    from paurubot.db.models import AuditLog, StaffRole

    client, shop = app_env["client"], app_env["shop"]
    plain = (await client.get("/api/webapp/me", headers=h(8501))).json()
    assert plain["staff"] is None
    assert (await client.get("/api/webapp/staff/dashboard", headers=h(8501))).status_code == 403
    await create_staff(
        session, shop.id, role=StaffRole.FINANCE, username="fin8502", password=None, telegram_id=8502
    )
    await create_staff(
        session, shop.id, role=StaffRole.SUPPORT, username="sup8503", password=None, telegram_id=8503
    )
    await session.commit()

    finance = (await client.get("/api/webapp/me", headers=h(8502))).json()["staff"]
    assert finance == {"role": "finance", "scoped": False, "dashboard": True, "review": True}
    support = (await client.get("/api/webapp/me", headers=h(8503))).json()["staff"]
    assert support["dashboard"] and not support["review"]
    assert (await client.get("/api/webapp/staff/reviews", headers=h(8503))).status_code == 403
    board = (await client.get("/api/webapp/staff/dashboard", headers=h(8503))).json()
    assert "revenue_today" in board and "series" not in board

    # a customer's card receipt shows up for finance and is approved from the Mini App
    started = await client.post(
        "/api/webapp/wallet/topup", headers=h(8504), json={"amount": 100_000, "method": "card"}
    )
    payment_id = started.json()["payment"]["payment_id"]
    await client.post(
        f"/api/webapp/payments/{payment_id}/receipt",
        headers=h(8504),
        files={"file": ("r.png", png_bytes(), "image/png")},
    )
    items = (await client.get("/api/webapp/staff/reviews", headers=h(8502))).json()["items"]
    assert [i["id"] for i in items] == [payment_id] and items[0]["has_receipt"]
    approved = await client.post(f"/api/webapp/staff/reviews/{payment_id}/approve", headers=h(8502))
    assert approved.status_code == 200 and approved.json()["status"] == "paid"
    again = await client.post(f"/api/webapp/staff/reviews/{payment_id}/approve", headers=h(8502))
    assert again.status_code == 409
    assert (await client.get("/api/webapp/staff/reviews", headers=h(8502))).json()["items"] == []
    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == "payment.approved", AuditLog.entity_id == payment_id)
    )
    assert entry is not None


async def test_marketing_preferences_require_auth_and_only_modify_self(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Customer

    client = app_env["client"]
    path = "/api/webapp/me/marketing"
    assert (await client.post(path, json={"marketing_opt_out": True})).status_code == 401
    own = (await client.get("/api/webapp/me", headers=h(81501))).json()
    other = (await client.get("/api/webapp/me", headers=h(81502))).json()
    assert own["preferences"] == {"marketing_opt_out": False, "changed_at": None}
    for bad in ("false", 1, None):
        assert (await client.post(path, headers=h(81501), json={"marketing_opt_out": bad})).status_code == 422
    assert (
        await client.post(
            path, headers=h(81501), json={"marketing_opt_out": True, "customer_id": other["id"]}
        )
    ).status_code == 422
    response = await client.post(path, headers=h(81501), json={"marketing_opt_out": True})
    assert response.status_code == 200 and response.json()["marketing_opt_out"] is True
    again = await client.post(path, headers=h(81501), json={"marketing_opt_out": True})
    assert again.json() == response.json()
    assert (await client.get("/api/webapp/me", headers=h(81501))).json()["preferences"] == response.json()
    assert (await client.get("/api/webapp/me", headers=h(81502))).json()["preferences"][
        "marketing_opt_out"
    ] is False
    customer = await session.get(Customer, own["id"])
    await session.refresh(customer)
    assert customer.marketing_opt_out is True and customer.alerts_muted is False
    assert customer.marketing_change_source == "miniapp"


async def test_usage_history_is_self_only_and_does_not_query_the_panel(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from paurubot.core import usage_history
    from paurubot.panels.base import PanelUser
    from paurubot.provisioning import lifecycle

    client, shop, panel = app_env["client"], app_env["shop"], app_env["panel"]
    owner = await factories.customer(session, shop, telegram_id=81601)
    service = Service(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=panel.id,
        panel_username="history-owned",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.flush()
    await usage_history.observe(
        session, service, PanelUser("history-owned", used_bytes=123, usage_known=True), source="poll"
    )
    await session.commit()
    refresh = AsyncMock(side_effect=AssertionError("history GET must not contact the panel"))
    monkeypatch.setattr(lifecycle, "refresh", refresh)
    path = f"/api/webapp/services/{service.id}/usage"
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers=h(81602))).status_code == 404
    for days in (7, 30):
        response = await client.get(path, headers=h(81601), params={"days": days})
        assert response.status_code == 200, response.text
        assert response.json()["points"][0]["used_bytes"] == 123
        assert response.json()["days"] == days
    assert (await client.get(path, headers=h(81601), params={"days": 14})).status_code == 422
    refresh.assert_not_awaited()
    service.status = ServiceStatus.DELETED
    await session.commit()
    assert (await client.get(path, headers=h(81601))).status_code == 404
