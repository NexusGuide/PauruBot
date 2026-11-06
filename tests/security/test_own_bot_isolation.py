"""Each dedicated own-panel bot is a world of its own (OPB): its reseller reviews only its own
receipts, edits only its own plans and hears only about its own shop, from the Mini App and from
the bot alike. Another reseller, or the same person in another bot, never reaches them."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import update

from paurubot.api import webapp_auth
from paurubot.api.app import create_app
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Review
from paurubot.config import get_settings
from paurubot.core import own_panel, resale
from paurubot.db.models import Job, JobStatus, PanelKind, Payment, PaymentStatus, Plan
from paurubot.notifications import handlers
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.security.secretbox import encrypt
from paurubot.security.tokens import sign_init_data
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import TOKEN as MAIN_TOKEN
from tests.fakes.telegram import callback_update, make_bot, message_update

pytestmark = pytest.mark.security
TOKEN_A = "887766554:AAH-own-panel-bot-a-token-for-tests-only"
TOKEN_B = "887766555:AAH-own-panel-bot-b-token-for-tests-only"
OWNER_A, OWNER_B, BUYER = 71001, 72001, 73001


def signed(uid: int, token: str) -> dict[str, str]:
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
async def two(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))

    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), f"bot_{token.split(':')[0]}"

    async def no_webhook(shop_id: int) -> None:
        return None

    monkeypatch.setattr(resale, "bot_identity", identity)
    monkeypatch.setattr(resale, "point_webhook", no_webhook)
    monkeypatch.setattr(get_settings(), "owner_telegram_ids", [])
    fake = FakeTokenPanel("pasarguard")
    monkeypatch.setattr(registry, "test_transports", _EveryPanel(transport(fake)))
    main = await factories.shop(session, "Main")
    panel = await factories.panel(session, main, PanelKind.PASARGUARD)
    account_plan = await factories.account_plan(session, main, panel)
    shops = {}
    for name, owner_tg, token in (("a", OWNER_A, TOKEN_A), ("b", OWNER_B, TOKEN_B)):
        owner = await factories.customer(session, main, telegram_id=owner_tg)
        account = await factories.panel_account(session, owner, account_plan)
        account.password_enc = encrypt("panel", "panel-password")
        shops[name] = await own_panel.connect(session, account, token, name.upper())
    # a buyer's card receipt waiting in B, and a plan of B
    buyer = await factories.customer(session, shops["b"], telegram_id=BUYER)
    payment = Payment(
        shop_id=shops["b"].id,
        customer_id=buyer.id,
        purpose="topup",
        provider="card",
        status=PaymentStatus.VERIFYING,
        amount=50_000,
        idempotency_key="own-isolation-1",
        receipt_file_id="file-b",
    )
    session.add(payment)
    plan_b = Plan(shop_id=shops["b"].id, title="B plan", price=1_000, days=30, data_gb=10)
    session.add(plan_b)

    async def main_shop(_session):  # type: ignore[no-untyped-def]
        return main

    monkeypatch.setattr(webapp_auth, "root_shop", main_shop)
    await session.commit()
    webapp_auth.forget_reseller_tokens()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    app = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=app, base_url="http://t") as client:
        yield {"client": client, "main": main, **shops, "payment": payment, "plan_b": plan_b, "bot": bot}
    notify_telegram.set_bot(None)
    webapp_auth.forget_reseller_tokens()
    registry._cache.clear()


async def test_another_reseller_never_reaches_a_receipt_or_plan_in_the_mini_app(session, two) -> None:  # type: ignore[no-untyped-def]
    client, payment, plan_b = two["client"], two["payment"], two["plan_b"]
    a = signed(OWNER_A, TOKEN_A)
    assert (await client.get("/api/webapp/staff/reviews", headers=a)).json()["items"] == []
    assert (await client.get("/api/webapp/own", headers=a)).json()["figures"]["pending_receipts"] == 0
    assert (await client.get(f"/api/webapp/staff/reviews/{payment.id}/receipt", headers=a)).status_code == 404
    assert (
        await client.post(f"/api/webapp/staff/reviews/{payment.id}/approve", headers=a)
    ).status_code == 404
    rejected = await client.post(
        f"/api/webapp/staff/reviews/{payment.id}/reject", headers=a, json={"reason": "x"}
    )
    assert rejected.status_code == 404
    body = {"title": "mine now", "data_gb": 1, "days": 1, "price": 1}
    assert (await client.put(f"/api/webapp/own/plans/{plan_b.id}", headers=a, json=body)).status_code == 404
    assert (await client.delete(f"/api/webapp/own/plans/{plan_b.id}", headers=a)).status_code == 404
    assert [p["id"] for p in (await client.get("/api/webapp/own/plans", headers=a)).json()["items"]] == []

    # reseller A opening B's bot is only a customer there
    in_b = signed(OWNER_A, TOKEN_B)
    assert (await client.get("/api/webapp/me", headers=in_b)).json()["staff"] is None
    assert (await client.get("/api/webapp/staff/reviews", headers=in_b)).status_code == 403
    assert (await client.get("/api/webapp/own", headers=in_b)).status_code == 403
    # and a reseller is nobody's staff in the main shop's Mini App
    assert (
        await client.get("/api/webapp/staff/reviews", headers=signed(OWNER_A, MAIN_TOKEN))
    ).status_code == 403

    await session.refresh(payment)
    await session.refresh(plan_b)
    assert payment.status == PaymentStatus.VERIFYING and plan_b.title == "B plan"

    # B, the owner of the receipt, sees it, approves it by hand and it is paid
    b = signed(OWNER_B, TOKEN_B)
    assert [i["id"] for i in (await client.get("/api/webapp/staff/reviews", headers=b)).json()["items"]] == [
        payment.id
    ]
    assert (
        await client.post(f"/api/webapp/staff/reviews/{payment.id}/approve", headers=b)
    ).status_code == 200
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID


async def test_another_reseller_cannot_approve_from_its_own_bot(session, two) -> None:  # type: ignore[no-untyped-def]
    payment = two["payment"]
    for shop, uid in ((two["a"], OWNER_A), (two["b"], OWNER_A)):  # A in its own bot, then A in B's bot
        dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)
        await dp.feed_update(two["bot"], message_update(uid, "/start"))
        await dp.feed_update(two["bot"], callback_update(uid, Review(action="ok", id=payment.id).pack()))
        await session.refresh(payment)
        assert payment.status == PaymentStatus.VERIFYING

    dp = build_dispatcher(MemoryStorage(), shop_id=two["b"].id)
    await dp.feed_update(two["bot"], message_update(OWNER_B, "/start"))
    await dp.feed_update(two["bot"], callback_update(OWNER_B, Review(action="ok", id=payment.id).pack()))
    await session.refresh(payment)
    assert payment.status == PaymentStatus.PAID


async def test_staff_alerts_of_a_bot_reach_only_its_own_reseller(session, two) -> None:  # type: ignore[no-untyped-def]
    for perm in ("payments.review", "tickets.read", "dashboard.view"):
        assert await handlers.staff_chats(session, two["a"].id, perm) == [OWNER_A]
        assert await handlers.staff_chats(session, two["b"].id, perm) == [OWNER_B]
        assert not {OWNER_A, OWNER_B} & set(await handlers.staff_chats(session, two["main"].id, perm))
