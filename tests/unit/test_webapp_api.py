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


async def test_profile_alert_settings_are_strict_self_only_and_independent(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Customer

    client = app_env["client"]
    path = "/api/webapp/me/alerts"
    assert (await client.post(path, json={"alerts_muted": True})).status_code == 401
    own = (await client.get("/api/webapp/me", headers=h(81701))).json()
    other = (await client.get("/api/webapp/me", headers=h(81702))).json()
    customer = await session.get(Customer, own["id"])
    customer.phone = "+989123456789"
    await session.commit()
    profile = (await client.get("/api/webapp/me", headers=h(81701))).json()
    assert profile["profile"]["phone_masked"] == "••••89"
    assert profile["profile"]["member_since"]
    assert "+989123456789" not in str(profile)
    for bad in ("false", 1, None):
        assert (await client.post(path, headers=h(81701), json={"alerts_muted": bad})).status_code == 422
    for extra in ({"customer_id": other["id"]}, {"name": "changed"}, {"phone": "123"}):
        assert (
            await client.post(path, headers=h(81701), json={"alerts_muted": True, **extra})
        ).status_code == 422
    for _ in range(2):
        response = await client.post(path, headers=h(81701), json={"alerts_muted": True})
        assert response.status_code == 200 and response.json() == {"alerts_muted": True}
    updated = (await client.get("/api/webapp/me", headers=h(81701))).json()
    assert updated["alerts_muted"] is True and updated["preferences"] == own["preferences"]
    assert updated["name"] == own["name"] and updated["telegram_id"] == own["telegram_id"]
    assert (await client.get("/api/webapp/me", headers=h(81702))).json()["alerts_muted"] is False
    assert (
        await client.post(
            "/api/webapp/me/locale", headers=h(81701), json={"locale": "fa", "customer_id": other["id"]}
        )
    ).status_code == 422


async def test_miniapp_tickets_auth_validation_ownership_and_closed_rules(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Ticket, TicketDepartment

    client, shop = app_env["client"], app_env["shop"]
    valid = {"subject": "Connection", "text": "Please help", "department_id": None}
    for method, path, body in (
        ("GET", "/api/webapp/tickets", None),
        ("POST", "/api/webapp/tickets", valid),
        ("GET", "/api/webapp/tickets/1", None),
        ("POST", "/api/webapp/tickets/1/reply", {"text": "hello"}),
        ("POST", "/api/webapp/tickets/1/close", {}),
    ):
        assert (await client.request(method, path, json=body)).status_code == 401
    owner = (await client.get("/api/webapp/me", headers=h(81801))).json()
    foreign = await factories.shop(session)
    foreign_department = TicketDepartment(shop_id=foreign.id, title="Foreign")
    inactive = TicketDepartment(shop_id=shop.id, title="Inactive", is_active=False)
    own_department = TicketDepartment(shop_id=shop.id, title="Technical")
    session.add_all([foreign_department, inactive, own_department])
    await session.commit()
    listing = (await client.get("/api/webapp/tickets", headers=h(81801))).json()
    assert listing["departments"] == [{"id": own_department.id, "title": "Technical"}]
    for bad in (
        {"subject": "   "},
        {"text": "\n "},
        {"text": "x" * 4001},
        {"subject": "x" * 129},
        {"customer_id": owner["id"]},
        {"internal": True},
        {"department_id": True},
    ):
        response = await client.post("/api/webapp/tickets", headers=h(81801), json={**valid, **bad})
        assert response.status_code == 422, response.text
    for department_id in (foreign_department.id, inactive.id):
        response = await client.post(
            "/api/webapp/tickets", headers=h(81801), json={**valid, "department_id": department_id}
        )
        assert response.status_code == 400 and response.json()["error"] == "ticket_department_unknown"
    response = await client.post(
        "/api/webapp/tickets", headers=h(81801), json={**valid, "department_id": own_department.id}
    )
    assert response.status_code == 200, response.text
    ticket_id = response.json()["ticket"]["id"]
    detail_path = f"/api/webapp/tickets/{ticket_id}"
    for suffix, method, body in (
        ("", "GET", None),
        ("/reply", "POST", {"text": "intrusion"}),
        ("/close", "POST", {}),
    ):
        assert (
            await client.request(method, detail_path + suffix, headers=h(81802), json=body)
        ).status_code == 404
    for body in ({"text": "   "}, {"text": "x", "internal": True}, {"text": "x", "author_type": "staff"}):
        assert (await client.post(detail_path + "/reply", headers=h(81801), json=body)).status_code == 422
    reply = await client.post(detail_path + "/reply", headers=h(81801), json={"text": "  More details  "})
    assert reply.status_code == 200
    thread = (await client.get(detail_path, headers=h(81801))).json()
    assert [m["text"] for m in thread["messages"]] == ["Please help", "More details"]
    assert all(m["from"] == "me" for m in thread["messages"])
    for _ in range(2):
        assert (await client.post(detail_path + "/close", headers=h(81801))).json()["ticket"][
            "status"
        ] == "closed"
    rejected = await client.post(detail_path + "/reply", headers=h(81801), json={"text": "Too late"})
    assert rejected.status_code == 409 and rejected.json()["error"] == "ticket_closed"
    # Defense in depth: even an inconsistent historical row with the same customer must match shop.
    inconsistent = Ticket(shop_id=foreign.id, customer_id=owner["id"], subject="Must stay private")
    session.add(inconsistent)
    await session.commit()
    assert (await client.get(f"/api/webapp/tickets/{inconsistent.id}", headers=h(81801))).status_code == 404
    assert inconsistent.id not in [
        t["id"] for t in (await client.get("/api/webapp/tickets", headers=h(81801))).json()["items"]
    ]


async def test_miniapp_ticket_cursor_pages_never_expose_internal_notes_or_file_ids(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Ticket, TicketMessage, TicketStatus

    client, shop = app_env["client"], app_env["shop"]
    own = (await client.get("/api/webapp/me", headers=h(81811))).json()
    rows = [
        Ticket(shop_id=shop.id, customer_id=own["id"], subject=f"Ticket {i}", status=TicketStatus.CLOSED)
        for i in range(22)
    ]
    session.add_all(rows)
    await session.flush()
    ticket = rows[-1]
    messages = [
        TicketMessage(ticket_id=ticket.id, author_type="staff", text=f"Visible {i}") for i in range(105)
    ]
    internal = TicketMessage(
        ticket_id=ticket.id,
        author_type="staff",
        text="PRIVATE STAFF NOTE",
        is_internal=True,
        attachment_file_id="SECRET_FILE_ID",
    )
    attachment = TicketMessage(
        ticket_id=ticket.id, author_type="staff", text=None, attachment_file_id="PUBLIC_FILE_TOKEN"
    )
    session.add_all([*messages, internal, attachment])
    await session.commit()
    listing = (await client.get("/api/webapp/tickets", headers=h(81811))).json()
    assert len(listing["items"]) == 20 and listing["next_before"] == rows[2].id
    second = (
        await client.get("/api/webapp/tickets", headers=h(81811), params={"before": listing["next_before"]})
    ).json()
    assert [t["id"] for t in second["items"]] == [rows[1].id, rows[0].id] and second["next_before"] is None
    path = f"/api/webapp/tickets/{ticket.id}"
    latest = (await client.get(path, headers=h(81811))).json()
    older = (await client.get(path, headers=h(81811), params={"before": latest["next_before"]})).json()
    visible = older["messages"] + latest["messages"]
    assert len(latest["messages"]) == 100 and len(older["messages"]) == 6 and older["next_before"] is None
    assert [m["id"] for m in visible] == [m.id for m in messages] + [attachment.id]
    assert latest["messages"][-1]["has_attachment"] is True
    assert all(m["from"] == "support" for m in visible)
    assert all(
        set(m) == {"id", "from", "text", "has_attachment", "attachment_expired", "created_at", "reaction"}
        for m in visible
    )
    assert (
        "PRIVATE STAFF NOTE" not in str(visible)
        and "SECRET_FILE_ID" not in str(visible)
        and "PUBLIC_FILE_TOKEN" not in str(visible)
    )
    for invalid_cursor_path in ("/api/webapp/tickets", path):
        assert (
            await client.get(invalid_cursor_path, headers=h(81811), params={"before": 0})
        ).status_code == 422


async def test_miniapp_open_ticket_limit_is_preserved(app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    for _ in range(3):
        assert (
            await client.post(
                "/api/webapp/tickets", headers=h(81821), json={"subject": "Help", "text": "Question"}
            )
        ).status_code == 200
    response = await client.post(
        "/api/webapp/tickets", headers=h(81821), json={"subject": "Help", "text": "Question"}
    )
    assert response.status_code == 400 and response.json()["error"] == "ticket_limit"


async def test_miniapp_help_is_authenticated_shop_scoped_and_local_only(
    session, app_env, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from paurubot.core import apps

    client, shop = app_env["client"], app_env["shop"]
    shop.bot_username = "MiniShop_bot"
    other = await factories.shop(session)
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "help_items": [{"title": "Own guide", "text": "Steps", "url": "https://example.com/guide"}],
            "download_apps": [
                {"name": "Own app", "platform": "android", "repo": "Owner/App", "pattern": "*.apk"}
            ],
        },
    )
    await shop_settings.set_many(
        session, other.id, {"help_items": [{"title": "OTHER SHOP PRIVATE", "text": "Must not leak"}]}
    )
    await session.commit()
    no_network = AsyncMock(
        side_effect=AssertionError("help GET must not fetch releases, files or Redis cache")
    )
    monkeypatch.setattr(apps, "latest", no_network)
    monkeypatch.setattr(apps, "cached", no_network)
    assert (await client.get("/api/webapp/help")).status_code == 401
    response = await client.get("/api/webapp/help", headers=h(81901), params={"shop_id": other.id})
    assert response.status_code == 200
    body = response.json()
    assert body["articles"][0]["title"] == "Own guide" and "OTHER SHOP PRIVATE" not in response.text
    assert body["apps"][0]["release_url"] == "https://github.com/Owner/App/releases"
    assert body["bot_url"] == "https://t.me/MiniShop_bot"
    assert set(body) == {"articles", "apps", "bot_url"}
    assert "pattern" not in response.text and "file_id" not in response.text
    no_network.assert_not_awaited()


async def test_topup_intent_replay_conflict_and_scoped_response(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client = app_env["client"]
    body = {"amount": 100_000, "method": "card", "intent_key": "api-topup-intent-1"}
    first = await client.post("/api/webapp/wallet/topup", headers=h(8701), json=body)
    second = await client.post("/api/webapp/wallet/topup", headers=h(8701), json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert "meta" not in first.json()["payment"]
    assert "intent_key" not in first.text and "checkout_intent_v1" not in first.text
    different = await client.post(
        "/api/webapp/wallet/topup", headers=h(8701), json={**body, "amount": 200_000}
    )
    assert different.status_code == 409 and different.json()["error"] == "payment_intent_conflict"
    other = await client.post("/api/webapp/wallet/topup", headers=h(8702), json=body)
    assert (
        other.status_code == 200
        and other.json()["payment"]["payment_id"] != first.json()["payment"]["payment_id"]
    )
    payment = await session.get(Payment, first.json()["payment"]["payment_id"])
    payment.status = PaymentStatus.EXPIRED
    await session.commit()
    closed = await client.post("/api/webapp/wallet/topup", headers=h(8701), json=body)
    assert closed.status_code == 409 and closed.json()["error"] == "payment_intent_closed"


@pytest.mark.parametrize("key", ["tiny", "with spaces", "x" * 129])
async def test_topup_intent_key_validation(app_env, key) -> None:  # type: ignore[no-untyped-def]
    response = await app_env["client"].post(
        "/api/webapp/wallet/topup",
        headers=h(8703),
        json={"amount": 100_000, "method": "card", "intent_key": key},
    )
    assert response.status_code == 422


async def test_ticket_media_owned_proxy_retry_and_cleanup(session, app_env) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from paurubot.core import ticket_media
    from paurubot.db.base import utcnow
    from paurubot.db.models import Job, Ticket, TicketMessage

    client = app_env["client"]
    key = "media-test-key-00001"
    request = {"subject": "Upload", "text": "Help", "request_key": "new-ticket-key-0001"}
    opened = await client.post("/api/webapp/tickets", headers=h(81901), json=request)
    assert opened.status_code == 200, opened.text
    ticket_id = opened.json()["ticket"]["id"]
    repeated = await client.post("/api/webapp/tickets", headers=h(81901), json=request)
    assert repeated.json()["ticket"]["id"] == ticket_id
    assert (
        await client.post("/api/webapp/tickets", headers=h(81901), json={**request, "text": "changed"})
    ).status_code == 409
    path = f"/api/webapp/tickets/{ticket_id}"
    headers = {**h(81901), "content-type": "image/png", "x-ticket-request-key": key}
    content = png_bytes()
    assert (await client.post(path + "/attachments", content=content, headers={})).status_code == 401
    assert (
        await client.post(path + "/attachments", content=content, headers={**headers, **h(81902)})
    ).status_code == 404
    assert (await client.post(path + "/attachments", content=content, headers=h(81901))).status_code == 422
    uploaded = await client.post(path + "/attachments", content=content, headers=headers)
    assert uploaded.status_code == 200, uploaded.text
    mid = uploaded.json()["message_id"]
    job_count = len(list(await session.scalars(select(Job))))
    for _ in range(2):
        retry = await client.post(path + "/attachments", content=content, headers=headers)
        assert retry.status_code == 200 and retry.json()["message_id"] == mid
    assert len(list(await session.scalars(select(Job)))) == job_count
    assert (
        await client.post(path + "/attachments", content=b"different", headers=headers)
    ).status_code == 409
    download = path + f"/messages/{mid}/attachment"
    assert (await client.get(download, headers=h(81902))).status_code == 404
    assert (await client.get(download)).status_code == 401
    result = await client.get(download, headers=h(81901))
    assert result.status_code == 200 and result.headers["content-type"] == "image/png"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "no-store" in result.headers["cache-control"]
    assert "attachment;" in result.headers["content-disposition"]
    internal = TicketMessage(
        ticket_id=ticket_id, author_type="staff", is_internal=True, attachment_file_id="never-fetch"
    )
    session.add(internal)
    await session.commit()
    assert (
        await client.get(path + f"/messages/{internal.id}/attachment", headers=h(81901))
    ).status_code == 404
    wrong = await client.post(
        "/api/webapp/tickets", headers=h(81901), json={"subject": "Other", "text": "Hi"}
    )
    wrong_id = wrong.json()["ticket"]["id"]
    assert (
        await client.get(f"/api/webapp/tickets/{wrong_id}/messages/{mid}/attachment", headers=h(81901))
    ).status_code == 404
    await client.post(path + "/close", headers=h(81901))
    assert (await client.post(path + "/attachments", content=content, headers=headers)).json()[
        "message_id"
    ] == mid
    assert (
        await client.post(
            path + "/attachments",
            content=content,
            headers={**headers, "x-ticket-request-key": "fresh-closed-key-001"},
        )
    ).status_code == 409
    ticket = await session.get(Ticket, ticket_id)
    ticket.updated_at = utcnow() - timedelta(days=31)
    await session.commit()
    assert await ticket_media.prune(session) == 1
    await session.commit()
    assert (await client.get(download, headers=h(81901))).status_code == 410
    # Expiration retains the replay tombstone: no duplicate and no resurrection.
    assert (await client.post(path + "/attachments", content=content, headers=headers)).json()[
        "message_id"
    ] == mid
    assert await session.scalar(select(TicketMessage.attachment_data).where(TicketMessage.id == mid)) is None


async def test_ticket_linked_service_minimal_snapshot_and_foreign_rejection(session, app_env) -> None:  # type: ignore[no-untyped-def]
    client, shop, panel = app_env["client"], app_env["shop"], app_env["panel"]
    own = (await client.get("/api/webapp/me", headers=h(81911))).json()
    service = Service(
        shop_id=shop.id,
        customer_id=own["id"],
        panel_id=panel.id,
        panel_username="DO_NOT_EXPOSE",
        subscription_url="https://private.invalid/secret",
        panel_data={"password": "secret"},
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.commit()
    body = {
        "subject": "Service issue",
        "text": "Help",
        "service_id": service.id,
        "request_key": "service-request-0001",
    }
    assert (await client.post("/api/webapp/tickets", headers=h(81912), json=body)).status_code == 404
    response = await client.post("/api/webapp/tickets", headers=h(81911), json=body)
    assert response.status_code == 200, response.text
    context = response.json()["ticket"]["service_context"]
    # REF-09 adds when it was read and from where; still no links, credentials or panel data.
    assert set(context) == {
        "id",
        "status",
        "used_bytes",
        "data_limit",
        "expire_at",
        "online_at",
        "synced_at",
        "checked",
        "panel_down",
    }
    assert context["id"] == service.id and context["status"] == "active"
    assert "DO_NOT_EXPOSE" not in response.text and "secret" not in response.text
    service.status = ServiceStatus.DISABLED
    await session.commit()
    replay = await client.post("/api/webapp/tickets", headers=h(81911), json=body)
    assert replay.json()["ticket"]["service_context"] == context
    other_shop = await factories.shop(session)
    service.shop_id = other_shop.id  # Inconsistent historical ownership must fail closed.
    await session.commit()
    assert (
        await client.post(
            "/api/webapp/tickets", headers=h(81911), json={**body, "request_key": "service-request-0002"}
        )
    ).status_code == 404


async def test_ticket_media_validation_quota_and_text_replay(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import ticket_media

    client = app_env["client"]
    opened = await client.post(
        "/api/webapp/tickets", headers=h(81921), json={"subject": "Limits", "text": "Hi"}
    )
    path = f"/api/webapp/tickets/{opened.json()['ticket']['id']}"
    headers = {**h(81921), "content-type": "text/plain", "x-ticket-request-key": "limits-request-0001"}
    for content, mime, status in (
        (b"<script>alert(1)</script>", "text/plain", 415),
        (b"%PDF", "application/pdf", 415),
        (b"", "text/plain", 413),
        (b"x" * (ticket_media.MAX_BYTES + 1), "text/plain", 413),
    ):
        assert (
            await client.post(
                path + "/attachments", content=content, headers={**headers, "content-type": mime}
            )
        ).status_code == status
    monkeypatch.setattr(ticket_media, "MAX_FILES", 0)
    assert (await client.post(path + "/attachments", content=b"log", headers=headers)).status_code == 409
    monkeypatch.setattr(ticket_media, "MAX_FILES", 16)
    assert (await client.post(path + "/attachments", content=b"log", headers=headers)).status_code == 200
    body = {"text": "reply", "request_key": "reply-request-00001"}
    assert (await client.post(path + "/reply", json=body, headers=h(81921))).status_code == 200
    assert (await client.post(path + "/reply", json=body, headers=h(81921))).status_code == 200
    assert (
        await client.post(path + "/reply", json={**body, "text": "changed"}, headers=h(81921))
    ).status_code == 409
    detail = (await client.get(path, headers=h(81921))).json()
    assert [m["text"] for m in detail["messages"]] == ["Hi", None, "reply"]


async def test_legacy_ticket_attachment_auth_precedes_provider_io(session, app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import TicketMessage

    client = app_env["client"]
    result = await client.post(
        "/api/webapp/tickets", headers=h(81931), json={"subject": "Old file", "text": "Hi"}
    )
    tid = result.json()["ticket"]["id"]
    message = TicketMessage(ticket_id=tid, author_type="staff", attachment_file_id="PRIVATE_TELEGRAM_ID")
    session.add(message)
    await session.commit()
    calls = []

    async def unavailable(shop_id):  # type: ignore[no-untyped-def]
        calls.append(shop_id)
        return None

    monkeypatch.setattr(notify_telegram, "bot_for", unavailable)
    path = f"/api/webapp/tickets/{tid}/messages/{message.id}/attachment"
    assert (await client.get(path, headers=h(81932))).status_code == 404
    assert calls == []
    response = await client.get(path, headers=h(81931))
    assert response.status_code == 503 and "PRIVATE_TELEGRAM_ID" not in response.text
    assert calls == [app_env["shop"].id]


async def test_ticket_media_byte_quotas_across_tickets(app_env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from paurubot.core import ticket_media

    client = app_env["client"]
    paths = []
    for subject in ("First", "Second"):
        response = await client.post(
            "/api/webapp/tickets", headers=h(81941), json={"subject": subject, "text": "Hi"}
        )
        assert response.status_code == 200, response.text
        paths.append(f"/api/webapp/tickets/{response.json()['ticket']['id']}/attachments")
    headers = {**h(81941), "content-type": "text/plain", "x-ticket-request-key": "quota-byte-test-0001"}
    monkeypatch.setattr(ticket_media, "MAX_TICKET_BYTES", 3)
    monkeypatch.setattr(ticket_media, "MAX_CUSTOMER_BYTES", 4)
    # Empty aggregate is zero, and an exact boundary is allowed.
    assert (await client.post(paths[0], content=b"log", headers=headers)).status_code == 200
    response = await client.post(
        paths[0], content=b"x", headers={**headers, "x-ticket-request-key": "quota-byte-test-0002"}
    )
    assert response.status_code == 409 and response.json()["error"] == "ticket_file_quota"
    # The second ticket has space, but the customer's aggregate cannot exceed four bytes.
    response = await client.post(paths[1], content=b"xx", headers=headers)
    assert response.status_code == 409 and response.json()["error"] == "ticket_file_quota"
    assert (await client.post(paths[1], content=b"x", headers=headers)).status_code == 200


async def test_gateway_names_follow_the_bot_texts(session, app_env) -> None:  # type: ignore[no-untyped-def]
    """A gateway renamed in the bot's texts is named the same in the Mini App (not its own name)."""
    from paurubot import i18n
    from paurubot.db.models import BotText

    client, shop = app_env["client"], app_env["shop"]
    me = (await client.get("/api/webapp/me", headers=h(7311))).json()
    assert me["payment_labels"] == {}  # nothing renamed: the app keeps its own names
    session.add(BotText(shop_id=shop.id, locale=me["locale"], key="btn_pay_card", value="💳 <b>Our card</b>"))
    session.add(BotText(shop_id=shop.id, locale=me["locale"], key="btn_pay_tonpay", value="🏦 Tonpay fast"))
    await session.commit()
    i18n.invalidate(shop.id)
    me = (await client.get("/api/webapp/me", headers=h(7311))).json()
    assert me["payment_labels"]["card"] == "Our card"  # no emoji, no markup
    assert "tonpay" not in me["payment_labels"]  # not one of this shop's methods
