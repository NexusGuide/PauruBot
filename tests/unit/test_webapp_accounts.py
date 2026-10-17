"""Panel admin accounts through the Mini App API (TEN-14): plans, buying, renewing, add-ons,
login details and refreshing usage — every endpoint scoped to its own customer."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import update

from paurubot.api import webapp_auth
from paurubot.api.app import create_app
from paurubot.core import settings as shop_settings
from paurubot.db.models import Job, JobStatus, PanelKind
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import GB, registry
from paurubot.worker.runner import QUEUES, run_job  # noqa: F401  (drain() needs them registered)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot
from tests.unit.test_resellers import drain
from tests.unit.test_webapp_api import h


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def acc_env(session, monkeypatch):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    fake = FakeTokenPanel("pasarguard")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.account_plan(session, shop, panel)
    await session.commit()

    async def our_shop(_session):  # type: ignore[no-untyped-def]
        return shop

    monkeypatch.setattr(webapp_auth, "root_shop", our_shop)
    bot, _sent = make_bot()
    notify_telegram.set_bot(bot)
    carrier = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=carrier, base_url="http://t") as client:
        yield {"client": client, "shop": shop, "plan": plan, "panel": panel, "fake": fake}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def _buyer(session, acc_env, uid: int, balance=10_000_000):  # type: ignore[no-untyped-def]
    client, shop = acc_env["client"], acc_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(uid))).json()
    await wallet.topup(session, shop.id, me["id"], balance, provider="card", payment_id=uid)
    await session.commit()
    return me


async def _bought(session, acc_env, uid: int):  # type: ignore[no-untyped-def]
    """Buy the plan straight through the Mini App and let the worker apply it."""
    client, plan = acc_env["client"], acc_env["plan"]
    created = await client.post(
        "/api/webapp/orders", headers=h(uid), json={"kind": "account_purchase", "account_plan_id": plan.id}
    )
    assert created.status_code == 200, created.text
    paid = await client.post(
        f"/api/webapp/orders/{created.json()['order']['id']}/pay", headers=h(uid), json={"method": "wallet"}
    )
    assert paid.json()["order"]["status"] == "paid"
    await session.commit()
    await drain(session, "provision")
    items = (await client.get("/api/webapp/accounts", headers=h(uid))).json()["items"]
    assert len(items) == 1
    return items[0]


async def test_plans_list_and_full_purchase(session, acc_env) -> None:  # type: ignore[no-untyped-def]
    client, plan, fake = acc_env["client"], acc_env["plan"], acc_env["fake"]
    uid = 9701
    me = await _buyer(session, acc_env, uid)
    assert me["panel_accounts"] is True

    views = (await client.get("/api/webapp/account-plans", headers=h(uid))).json()
    assert views["available"] is True and [p["id"] for p in views["plans"]] == [plan.id]
    assert views["plans"][0]["gb_price"] == plan.gb_price
    assert views["plans"][0]["max_users"] == plan.max_users

    account = await _bought(session, acc_env, uid)
    sold = fake.sold_admins[account["username"]]
    assert sold["role_id"] == 2 and sold["data_limit"] == plan.data_gb * GB
    assert account["status"] == "active" and account["plan_title"] == plan.title
    assert account["expire_at"] is not None

    login = (await client.get(f"/api/webapp/accounts/{account['id']}/login", headers=h(uid))).json()
    assert login["username"] == account["username"] and login["password"]
    assert login["url"].startswith("https://panel.example.com")

    refreshed = await client.post(f"/api/webapp/accounts/{account['id']}/refresh", headers=h(uid))
    assert refreshed.status_code == 200 and refreshed.json()["account"]["id"] == account["id"]


async def test_renew_and_addons_are_ordinary_orders(session, acc_env) -> None:  # type: ignore[no-untyped-def]
    client, plan = acc_env["client"], acc_env["plan"]
    uid = 9702
    await _buyer(session, acc_env, uid)
    account = await _bought(session, acc_env, uid)
    for body, amount in (
        ({"kind": "account_renew", "account_id": account["id"]}, plan.price),
        ({"kind": "account_gb", "account_id": account["id"], "account_amount": 50}, 50 * plan.gb_price),
        ({"kind": "account_users", "account_id": account["id"], "account_amount": 10}, 10 * plan.user_price),
    ):
        order = await client.post("/api/webapp/orders", headers=h(uid), json=body)
        assert order.status_code == 200, order.text
        assert order.json()["order"]["amount"] == amount
        paid = await client.post(
            f"/api/webapp/orders/{order.json()['order']['id']}/pay", headers=h(uid), json={"method": "wallet"}
        )
        assert paid.json()["order"]["status"] == "paid"
        await session.commit()
        await drain(session, "provision")

    after = (await client.get(f"/api/webapp/accounts/{account['id']}", headers=h(uid))).json()["account"]
    assert after["data_limit"] == plan.data_gb * GB + 50 * GB
    assert after["max_users"] == plan.max_users + 10


async def test_unknown_ids_and_limits_are_rejected(session, acc_env) -> None:  # type: ignore[no-untyped-def]
    client, plan = acc_env["client"], acc_env["plan"]
    uid = 9711
    await client.get("/api/webapp/me", headers=h(uid))
    bad_plan = await client.post(
        "/api/webapp/orders", headers=h(uid), json={"kind": "account_purchase", "account_plan_id": 987_654}
    )
    assert bad_plan.status_code == 404 and bad_plan.json()["error"] == "plan_unavailable"
    bad_account = await client.post(
        "/api/webapp/orders", headers=h(uid), json={"kind": "account_renew", "account_id": 987_654}
    )
    assert bad_account.status_code == 404 and bad_account.json()["error"] == "account_not_found"
    huge = await client.post(
        "/api/webapp/orders",
        headers=h(uid),
        json={"kind": "account_purchase", "account_plan_id": plan.id, "account_amount": 400_000},
    )
    assert huge.status_code == 422


async def test_customers_cannot_touch_each_others_accounts(session, acc_env) -> None:  # type: ignore[no-untyped-def]
    client = acc_env["client"]
    uid, other = 9721, 9722
    await _buyer(session, acc_env, uid)
    account = await _bought(session, acc_env, uid)
    await client.get("/api/webapp/me", headers=h(other))
    for method, path in (
        ("GET", f"/api/webapp/accounts/{account['id']}"),
        ("GET", f"/api/webapp/accounts/{account['id']}/login"),
        ("POST", f"/api/webapp/accounts/{account['id']}/refresh"),
    ):
        resp = await client.request(method, path, headers=h(other))
        assert resp.status_code == 404, path
    order = await client.post(
        "/api/webapp/orders", headers=h(other), json={"kind": "account_renew", "account_id": account["id"]}
    )
    assert order.status_code == 404
    assert (await client.get("/api/webapp/accounts", headers=h(other))).json()["items"] == []


async def test_disabled_feature_is_hidden_and_unbuyable(session, acc_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, plan = acc_env["client"], acc_env["shop"], acc_env["plan"]
    uid = 9731
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": False})
    await session.commit()
    me = (await client.get("/api/webapp/me", headers=h(uid))).json()
    assert me["panel_accounts"] is False
    views = (await client.get("/api/webapp/account-plans", headers=h(uid))).json()
    assert views["available"] is False
    order = await client.post(
        "/api/webapp/orders", headers=h(uid), json={"kind": "account_purchase", "account_plan_id": plan.id}
    )
    assert order.status_code == 400 and order.json()["error"] == "accounts_unavailable"
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
