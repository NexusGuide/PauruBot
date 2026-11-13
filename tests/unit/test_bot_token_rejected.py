"""A customer's or reseller's bot whose token Telegram rejects is reported to the reports group.

The report goes out through the main shop (the dead bot cannot send anything) and says which bot
it is and whose: its @username and title, and its owner with a «manage user» button.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from paurubot.db.models import Job, ResellerProfile, Shop
from paurubot.notifications import handlers
from paurubot.worker import queue
from tests import factories


def _job(shop_id: int) -> queue.Claimed:
    return queue.Claimed(
        id=0,
        kind="bot_token_rejected",
        payload={"shop_id": shop_id},
        attempts=1,
        max_attempts=8,
        shop_id=shop_id,
        token="t",
    )


async def _capture(monkeypatch) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []

    async def report(session, shop_id, topic, text, *, markup=None):  # type: ignore[no-untyped-def]
        sent.append({"shop_id": shop_id, "topic": topic, "text": text, "markup": markup})

    monkeypatch.setattr(handlers, "report", report)
    return sent


async def test_a_reseller_bot_is_reported_with_its_owner(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    main = await factories.shop(session, "Main")
    main.bot_username = "main_shop_bot"
    owner = await factories.customer(session, main, telegram_id=555001, username="reza_seller")
    profile = ResellerProfile(shop_id=main.id, customer_id=owner.id)
    session.add(profile)
    await session.flush()
    child = await factories.shop(session, "Reza VPN")
    child.reseller_profile_id = profile.id
    child.bot_username = "reza_vpn_bot"
    await session.commit()
    sent = await _capture(monkeypatch)

    await handlers.bot_token_rejected(session, _job(child.id))

    [report] = sent
    assert report["shop_id"] == main.id and report["topic"] == "reports"
    assert "@reza_vpn_bot" in report["text"] and "Reza VPN" in report["text"]
    assert "@reza_seller" in report["text"] and "555001" in report["text"]
    assert report["markup"] is not None, "a «manage user» button to the owner"


async def test_a_customers_own_bot_is_reported_with_its_owner(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    main = await factories.shop(session, "Main")
    panel = await factories.panel(session, main)
    plan = await factories.account_plan(session, main, panel)
    owner = await factories.customer(session, main, telegram_id=555002, username="sara_own")
    account = await factories.panel_account(session, owner, plan)
    child = await factories.shop(session, "Sara Net")
    child.own_account_id = account.id
    child.bot_username = "sara_net_bot"
    await session.commit()
    sent = await _capture(monkeypatch)

    await handlers.bot_token_rejected(session, _job(child.id))

    [report] = sent
    assert report["shop_id"] == main.id
    assert "@sara_net_bot" in report["text"] and "@sara_own" in report["text"]


async def test_the_polling_side_queues_one_report_a_day(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager

    from paurubot.bot import polling
    from paurubot.db import session as db_session

    shop = await factories.shop(session, "Child")
    await session.commit()

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield session
        await session.commit()

    monkeypatch.setattr(db_session, "session_scope", scope)
    await polling.report_rejected(shop.id)
    await polling.report_rejected(shop.id)
    jobs = list(await session.scalars(select(Job).where(Job.kind == "bot_token_rejected")))
    assert [j.payload for j in jobs] == [{"shop_id": shop.id}]
    assert jobs[0].queue == "notify"
    assert await session.get(Shop, shop.id) is not None
