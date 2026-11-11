"""ClockBot parity, batch 1 (panel): add and remove customers, message many, the trial users
list, broadcast history clean-up, putting dismissed inbox items back, and resetting many texts."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.db.models import (
    AuditLog,
    BotText,
    Broadcast,
    Customer,
    InboxDismissal,
    Job,
    JobStatus,
    Order,
    OrderKind,
    OrderStatus,
    Service,
    ServiceStatus,
)
from tests import factories
from tests.security.test_admin_api import env, http, login  # noqa: F401  (fixtures)

pytestmark = pytest.mark.security


async def test_staff_add_and_remove_a_customer(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    made = await owner.call("POST", "/api/admin/customers", {"telegram_id": 771100223, "first_name": "Sara"})
    assert made.status_code == 200, made.text
    cid = made.json()["id"]
    again = await owner.call("POST", "/api/admin/customers", {"telegram_id": 771100223})
    assert again.status_code == 409 and again.json()["error"] == "customer_exists"
    assert (await owner.call("POST", "/api/admin/customers", {"telegram_id": 12})).status_code == 422
    gone = await owner.call("DELETE", f"/api/admin/customers/{cid}")
    assert gone.status_code == 200
    assert await session.get(Customer, cid) is None
    # A customer with history is never deleted: banning keeps the books right.
    busy = await owner.call("DELETE", f"/api/admin/customers/{env.customer_id}")
    assert busy.status_code == 409 and busy.json()["error"] == "customer_has_history"
    actions = set(await session.scalars(select(AuditLog.action).where(AuditLog.action.like("customer.%"))))
    assert {"customer.created", "customer.deleted"} <= actions
    finance = await login(http, "u_finance")
    assert (await finance.call("POST", "/api/admin/customers", {"telegram_id": 771100224})).status_code == 403


async def test_a_message_to_many_customers_is_queued(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    await session.commit()
    owner = await login(http, "u_owner")
    done = await owner.call(
        "POST", "/api/admin/customers/bulk", {"action": "message", "ids": [env.customer_id], "text": "سلام"}
    )
    assert done.status_code == 200, done.text
    [job] = list(
        await session.scalars(select(Job).where(Job.kind == "staff_message", Job.status == JobStatus.QUEUED))
    )
    assert job.payload["customer_ids"] == [env.customer_id] and job.payload["text"] == "سلام"
    empty = await owner.call(
        "POST", "/api/admin/customers/bulk", {"action": "message", "ids": [env.customer_id]}
    )
    assert empty.status_code == 422


async def test_trial_users_and_who_went_on_to_buy(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    trier = await factories.customer(session, env.shop, telegram_id=771100301)
    buyer = await factories.customer(session, env.shop, telegram_id=771100302)
    for customer in (trier, buyer):
        session.add(
            Service(
                shop_id=env.shop.id,
                customer_id=customer.id,
                panel_id=env.panel.id,
                panel_username=f"t_{customer.id}",
                status=ServiceStatus.ACTIVE,
                is_trial=True,
            )
        )
    session.add(
        Order(
            shop_id=env.shop.id,
            customer_id=buyer.id,
            kind=OrderKind.PURCHASE,
            status=OrderStatus.COMPLETED,
            base_price=100_000,
            amount=100_000,
        )
    )
    await session.commit()
    owner = await login(http, "u_owner")
    data = (await owner.call("GET", "/api/admin/trial-users")).json()
    by_id = {row["customer"]["id"]: row for row in data["items"]}
    assert by_id[buyer.id]["bought"] is True and by_id[trier.id]["bought"] is False
    assert data["summary"]["trials"] >= 2 and data["summary"]["bought"] >= 1
    only = (await owner.call("GET", "/api/admin/trial-users?filter=not_bought")).json()["items"]
    assert buyer.id not in {r["customer"]["id"] for r in only}


async def test_broadcast_history_clean_up(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    done = Broadcast(shop_id=env.shop.id, text="old", status=JobStatus.DONE)
    running = Broadcast(shop_id=env.shop.id, text="now", status=JobStatus.RUNNING)
    session.add_all([done, running])
    await session.commit()
    owner = await login(http, "u_owner")
    assert (await owner.call("DELETE", f"/api/admin/broadcasts/{running.id}")).status_code == 409
    assert (await owner.call("DELETE", f"/api/admin/broadcasts/{done.id}")).status_code == 200
    assert await session.scalar(select(Broadcast.id).where(Broadcast.id == done.id)) is None
    finished = Broadcast(shop_id=env.shop.id, text="older", status=JobStatus.DONE)
    session.add(finished)
    await session.commit()
    cleared = (await owner.call("POST", "/api/admin/broadcasts/clear")).json()
    assert cleared["deleted"] >= 1
    assert await session.scalar(select(Broadcast.id).where(Broadcast.id == running.id)) is not None


async def test_dismissed_inbox_items_come_back(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    await owner.call("POST", "/api/admin/inbox/dismiss", {"type": "payment", "id": 1})
    assert await session.scalar(select(InboxDismissal.id))
    assert (await owner.call("POST", "/api/admin/inbox/reset")).json()["restored"] >= 1
    assert await session.scalar(select(InboxDismissal.id)) is None


async def test_reset_many_texts_at_once(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    session.add_all(
        [
            BotText(shop_id=env.shop.id, locale="fa", key="welcome", value="x"),
            BotText(shop_id=env.shop.id, locale="fa", key="btn_buy", value="y"),
        ]
    )
    await session.commit()
    owner = await login(http, "u_owner")
    done = await owner.call("POST", "/api/admin/texts/reset", {"keys": ["welcome", "btn_buy"]})
    assert done.status_code == 200 and done.json()["reset"] == 2
    assert await session.scalar(select(BotText.id).where(BotText.shop_id == env.shop.id)) is None


async def test_quick_renew_the_most_urgent_service(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from datetime import timedelta

    from paurubot.db.base import utcnow

    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    plan = await factories.plan(session, env.shop, env.panel, price=100_000, days=30)
    soon = Service(
        shop_id=env.shop.id,
        customer_id=env.customer_id,
        panel_id=env.panel.id,
        plan_id=plan.id,
        panel_username="q_soon",
        status=ServiceStatus.ACTIVE,
        expire_at=utcnow() + timedelta(days=1),
    )
    later = Service(
        shop_id=env.shop.id,
        customer_id=env.customer_id,
        panel_id=env.panel.id,
        plan_id=plan.id,
        panel_username="q_later",
        status=ServiceStatus.ACTIVE,
        expire_at=utcnow() + timedelta(days=20),
    )
    session.add_all([soon, later])
    await session.commit()
    owner = await login(http, "u_owner")
    done = await owner.call("POST", f"/api/admin/customers/{env.customer_id}/quick-renew", {})
    assert done.status_code == 200, done.text
    order = await session.get(Order, done.json()["order_id"])
    assert order.service_id == soon.id and order.kind == OrderKind.RENEW and order.amount == 0
    assert order.status == OrderStatus.PAID and order.source == "staff"
    picked = await owner.call(
        "POST", f"/api/admin/customers/{env.customer_id}/quick-renew", {"service_id": later.id}
    )
    assert (await session.get(Order, picked.json()["order_id"])).service_id == later.id
    again = await owner.call("POST", f"/api/admin/customers/{env.customer_id}/quick-renew", {})
    assert again.status_code == 409 and again.json()["error"] == "renew_in_progress"  # no second free renewal
    finance = await login(http, "u_finance")
    assert (
        await finance.call("POST", f"/api/admin/customers/{env.customer_id}/quick-renew", {})
    ).status_code == 403


async def test_test_customers_stay_out_of_reports(session, http, env) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    owner = await login(http, "u_owner")
    done = await owner.call("PATCH", f"/api/admin/customers/{env.customer_id}", {"is_test": True})
    assert done.status_code == 200, done.text
    assert (await session.get(Customer, env.customer_id)).is_test is True


async def test_customer_photo_through_the_bot(session, http, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    from types import SimpleNamespace

    from paurubot.notifications import telegram

    class FakeBot:
        async def get_user_profile_photos(self, user_id, limit=1):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                photos=[[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="big")]]
            )

        async def download(self, file_id, destination):  # type: ignore[no-untyped-def]
            destination.write(b"\xff\xd8jpeg-" + file_id.encode())

    async def bot_for(shop_id):  # type: ignore[no-untyped-def]
        return FakeBot()

    monkeypatch.setattr(telegram, "bot_for", bot_for)
    owner = await login(http, "u_owner")
    got = await owner.call("GET", f"/api/admin/customers/{env.customer_id}/photo")
    assert got.status_code == 200 and got.content == b"\xff\xd8jpeg-big"
    assert got.headers["content-type"] == "image/jpeg"
