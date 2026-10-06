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
