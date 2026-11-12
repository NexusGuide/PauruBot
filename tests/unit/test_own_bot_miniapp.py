"""The reseller runs its dedicated own-panel bot from that bot's Mini App (OPB-4): plans, card,
features and receipts, and only there."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from paurubot.api import webapp_auth
from paurubot.api.app import create_app
from paurubot.core import orders, own_panel, resale
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    Job,
    JobStatus,
    Order,
    OrderKind,
    Panel,
    PanelKind,
    Plan,
    Service,
    ServiceStatus,
    StaffRole,
)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.security.secretbox import encrypt
from paurubot.security.tokens import sign_init_data
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import TOKEN as MAIN_TOKEN
from tests.fakes.telegram import make_bot, png_bytes

CHILD_TOKEN = "887766554:AAH-own-panel-bot-token-for-tests-only-x"
OWNER_TG = 71001
BUYER_TG = 71501
CARD = "6037991234567893"  # passes the Luhn check
GB = 1024**3


def signed(uid: int, token: str = CHILD_TOKEN) -> dict[str, str]:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "AA",
        "user": json.dumps({"id": uid, "first_name": "Sara", "language_code": "en"}),
    }
    return {"x-telegram-init-data": sign_init_data(fields, token)}


class _EveryPanel(dict):  # type: ignore[type-arg]
    def __init__(self, shared) -> None:  # type: ignore[no-untyped-def]
        super().__init__()
        self.shared = shared

    def get(self, key, default=None):  # type: ignore[no-untyped-def]
        return self.shared


@pytest.fixture
async def own(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))

    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "ali_own_bot"

    async def no_webhook(shop_id: int) -> None:
        return None

    monkeypatch.setattr(resale, "bot_identity", identity)
    monkeypatch.setattr(resale, "point_webhook", no_webhook)
    fake = FakeTokenPanel("pasarguard")
    fake.groups = [{"id": 1, "name": "Germany-1"}, {"id": 2, "name": "Turkey"}]
    monkeypatch.setattr(registry, "test_transports", _EveryPanel(transport(fake)))
    shop = await factories.shop(session, "Main")
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    account_plan = await factories.account_plan(session, shop, panel)
    owner = await factories.customer(session, shop, telegram_id=OWNER_TG)
    account = await factories.panel_account(session, owner, account_plan)
    account.password_enc = encrypt("panel", "panel-password")
    fake.sold_admins[account.username] = {"username": account.username, "status": "active"}
    child = await own_panel.connect(session, account, CHILD_TOKEN, "Ali shop")

    async def main_shop(_session):  # type: ignore[no-untyped-def]
        return shop

    monkeypatch.setattr(webapp_auth, "root_shop", main_shop)
    await session.commit()
    webapp_auth.forget_reseller_tokens()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    app = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=app, base_url="http://t") as client:
        yield {"client": client, "shop": shop, "child": child, "account": account, "fake": fake, "tg": tg}
    notify_telegram.set_bot(None)
    webapp_auth.forget_reseller_tokens()
    registry._cache.clear()


async def test_the_merchant_sees_its_bot_and_the_buyers_do_not(session, own) -> None:  # type: ignore[no-untyped-def]
    client = own["client"]
    me = (await client.get("/api/webapp/me", headers=signed(OWNER_TG))).json()
    assert me["staff"]["role"] == StaffRole.MERCHANT.value and me["staff"]["own"] is True
    view = (await client.get("/api/webapp/own", headers=signed(OWNER_TG))).json()
    assert view["account"]["username"] == own["account"].username
    assert view["paused"] == "" and view["hold"] is False and view["card"] is None
    assert view["plans"] == {"active": 0, "total": 0}

    buyer = (await client.get("/api/webapp/me", headers=signed(BUYER_TG))).json()
    assert buyer["staff"] is None
    for method, path in (("GET", "/api/webapp/own"), ("GET", "/api/webapp/own/plans")):
        assert (await client.request(method, path, headers=signed(BUYER_TG))).status_code == 403
    # the same person in the main shop's Mini App is an ordinary customer there
    assert (await client.get("/api/webapp/own", headers=signed(OWNER_TG, MAIN_TOKEN))).status_code == 403


async def test_plans_are_made_on_the_resellers_own_panel_and_groups(session, own) -> None:  # type: ignore[no-untyped-def]
    client, h = own["client"], signed(OWNER_TG)
    listed = (await client.get("/api/webapp/own/plans", headers=h)).json()
    assert listed["groups_ok"] and [g["title"] for g in listed["groups"]] == ["Germany-1", "Turkey"]

    body = {"title": "30 GB month", "data_gb": 30, "days": 30, "price": 120_000, "group_ids": ["1"]}
    made = await client.post("/api/webapp/own/plans", headers=h, json=body)
    assert made.status_code == 200, made.text
    plan = await session.get(Plan, made.json()["plan"]["id"])
    own_panel_row = await session.scalar(select(Panel).where(Panel.shop_id == own["child"].id))
    assert plan is not None and own_panel_row is not None
    assert plan.shop_id == own["child"].id and plan.panel_id == own_panel_row.id
    assert plan.panel_options == {"group_ids": [1]}

    refused = await client.post("/api/webapp/own/plans", headers=h, json={**body, "group_ids": ["9"]})
    assert refused.status_code == 422 and refused.json()["error"] == "own_group_not_allowed"

    hidden = await client.put(
        f"/api/webapp/own/plans/{plan.id}", headers=h, json={**body, "is_active": False, "group_ids": []}
    )
    assert hidden.json()["plan"]["is_active"] is False and hidden.json()["plan"]["group_ids"] == []
    # a plan of another shop is out of reach
    other = await factories.plan(session, own["shop"], own["account"].panel, price=1)
    await session.commit()
    assert (await client.put(f"/api/webapp/own/plans/{other.id}", headers=h, json=body)).status_code == 404
    assert (await client.delete(f"/api/webapp/own/plans/{plan.id}", headers=h)).json() == {"deleted": True}
    actions = set(await session.scalars(select(AuditLog.action).where(AuditLog.shop_id == own["child"].id)))
    assert {"own.plan.create", "own.plan.update", "own.plan.delete"} <= actions


async def test_the_card_is_the_resellers_and_every_receipt_waits_for_it(session, own) -> None:  # type: ignore[no-untyped-def]
    client, h = own["client"], signed(OWNER_TG)
    bad = await client.put(
        "/api/webapp/own/payment",
        headers=h,
        json={"number": "1234", "holder": "Ali", "order_expire_minutes": 60},
    )
    assert bad.status_code == 422 and bad.json()["error"] == "card_number_invalid"
    saved = await client.put(
        "/api/webapp/own/payment",
        headers=h,
        json={"number": "6037 9912 3456 7893", "holder": "Ali Rezaei", "order_expire_minutes": 30},
    )
    assert saved.status_code == 200, saved.text
    cfg = await shop_settings.load(session, own["child"].id, fresh=True)
    assert cfg["card_accounts"] == [{"number": CARD, "holder": "Ali Rezaei"}]
    assert cfg["card_auto_approve"] is False and cfg["order_expire_minutes"] == 30
    main = await shop_settings.load(session, own["shop"].id, fresh=True)
    assert main["card_accounts"] != cfg["card_accounts"]  # the main shop's card is untouched


async def test_a_buyer_pays_the_reseller_and_approval_delivers_from_its_panel(session, own) -> None:  # type: ignore[no-untyped-def]
    client, h = own["client"], signed(OWNER_TG)
    await client.put(
        "/api/webapp/own/payment",
        headers=h,
        json={"number": CARD, "holder": "Ali Rezaei", "order_expire_minutes": 60},
    )
    plan_id = (
        await client.post(
            "/api/webapp/own/plans",
            headers=h,
            json={"title": "30 GB", "data_gb": 30, "days": 30, "price": 120_000, "group_ids": ["2"]},
        )
    ).json()["plan"]["id"]

    buyer = signed(BUYER_TG)
    order = await client.post(
        "/api/webapp/orders", headers=buyer, json={"kind": "purchase", "plan_id": plan_id}
    )
    assert order.status_code == 200, order.text
    order_id = order.json()["order"]["id"]
    started = await client.post(f"/api/webapp/orders/{order_id}/pay", headers=buyer, json={"method": "card"})
    assert started.status_code == 200, started.text
    shown = json.dumps(started.json())
    assert CARD in shown or CARD[-4:] in shown  # the buyer is shown the reseller's own card
    payment_id = started.json()["payment"]["payment_id"]
    sent = await client.post(
        f"/api/webapp/payments/{payment_id}/receipt",
        headers=buyer,
        files={"file": ("r.png", png_bytes(), "image/png")},
    )
    assert sent.status_code == 200, sent.text

    items = (await client.get("/api/webapp/staff/reviews", headers=h)).json()["items"]
    assert [i["id"] for i in items] == [payment_id]
    assert (await client.get("/api/webapp/own", headers=h)).json()["figures"]["pending_receipts"] == 1
    approved = await client.post(f"/api/webapp/staff/reviews/{payment_id}/approve", headers=h)
    assert approved.status_code == 200, approved.text

    from tests.unit.test_provisioning import drain

    await drain(session, "provision")
    service = await session.scalar(select(Service).where(Service.shop_id == own["child"].id))
    assert service is not None and service.panel is not None and service.panel.shop_id == own["child"].id
    created = own["fake"].users[service.panel_username]
    assert created["request"]["group_ids"] == [2]
    assert service.panel_username.startswith("sara")  # the buyer's name and a random tail
    assert (await session.get(Order, order_id)).kind == OrderKind.PURCHASE


async def test_features_set_trial_renewals_and_extra_ranges(session, own) -> None:  # type: ignore[no-untyped-def]
    client, h = own["client"], signed(OWNER_TG)
    body = {
        "trial": {"enabled": True, "gb": 1, "days": 1, "daily_quota": 20},
        "renew": {"enabled": False, "carry": True},
        "extra_gb": {"enabled": True, "price": 4_000, "min": 5, "max": 40},
        "extra_days": {"enabled": False, "price": 3_000, "min": 0, "max": 0},
    }
    saved = await client.put("/api/webapp/own/features", headers=h, json=body)
    assert saved.status_code == 200, saved.text
    features = saved.json()["features"]
    assert features["trial"] == body["trial"] and features["renew"] == {"enabled": False, "carry": True}
    assert features["extra_gb"]["enabled"] and not features["extra_days"]["enabled"]
    cfg = await shop_settings.load(session, own["child"].id, fresh=True)
    assert cfg["renew_mode"] == "extend_carry" and cfg["trial_daily_quota"] == 20
    assert orders.extra_choices(cfg, "gb") == [5, 10, 20, 40]
    panel = await session.scalar(select(Panel).where(Panel.shop_id == own["child"].id))
    assert panel is not None and panel.options["allow_renewals"] is False
    assert panel.options["sell_extra_days"] is False and panel.options["sell_extra_volume"] is True

    upside = {**body, "extra_gb": {"enabled": True, "price": 4_000, "min": 50, "max": 10}}
    assert (await client.put("/api/webapp/own/features", headers=h, json=upside)).json()[
        "error"
    ] == "own_extra_range"
    free = {**body, "extra_gb": {"enabled": True, "price": 0, "min": 0, "max": 0}}
    assert (await client.put("/api/webapp/own/features", headers=h, json=free)).json()[
        "error"
    ] == "own_extra_price"


def test_extra_bounds_keep_the_usual_choices_when_the_shop_sets_none() -> None:
    cfg = {"extra_gb_min": 0, "extra_gb_max": 0, "extra_day_min": 0, "extra_day_max": 0}
    assert orders.extra_choices(cfg, "gb") == [5, 10, 20, 50, 100]
    assert orders.extra_choices(cfg, "day") == [7, 15, 30, 60, 90]
    assert orders.extra_bounds(cfg, "gb") == (1, 10_000)
    assert orders.extra_choices({**cfg, "extra_day_min": 25}, "day") == [25, 30, 60, 90]
    assert orders.extra_choices({**cfg, "extra_gb_min": 3, "extra_gb_max": 4}, "gb") == [3, 4]


async def test_an_extra_outside_the_range_is_refused(session) -> None:  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Ranges")
    await shop_settings.set_many(session, shop.id, {"extra_gb_min": 5, "extra_gb_max": 40})
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    customer = await factories.customer(session, shop, telegram_id=73501)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="u_ranges",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * GB,
        expire_at=utcnow() + timedelta(days=15),
    )
    session.add(service)
    await session.commit()
    for amount in (4, 41):
        with pytest.raises(orders.OrderError):
            await orders.draft(session, customer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=amount)
    made = await orders.draft(session, customer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=40)
    assert made.params["extra_gb"] == 40


async def test_an_extra_admin_manages_the_bot_like_its_owner(session, own) -> None:  # type: ignore[no-untyped-def]
    client, child = own["client"], own["child"]
    helper = 71777
    assert (await client.get("/api/webapp/own", headers=signed(helper))).status_code == 403
    await own_panel.add_admin(session, child, helper)
    await session.commit()
    assert (await client.get("/api/webapp/own", headers=signed(helper))).status_code == 200
    await own_panel.remove_admin(session, child, helper)
    await session.commit()
    assert (await client.get("/api/webapp/own", headers=signed(helper))).status_code == 403
