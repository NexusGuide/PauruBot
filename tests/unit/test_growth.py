"""Loyalty levels, lucky wheel, lotteries and the trust channel (GRW-02, GRW-03, GRW-04, GRW-08)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from paurubot.core import growth, orders, pricing
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import money
from paurubot.db.base import utcnow
from paurubot.db.models import (
    DiscountCode,
    Job,
    JobStatus,
    Lottery,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    Service,
    TrustChannel,
    WheelSpin,
)
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import handlers as _handlers  # noqa: F401  (registers the notify queue)
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from paurubot.worker import jobs as worker_jobs  # noqa: F401  (registers queues)
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot
from tests.unit.test_service_lifecycle import _buy, drain

TIERS = [
    {"title": "Silver", "min_spent": 100_000, "percent": 5},
    {"title": "Gold", "min_spent": 300_000, "percent": 10},
]
FA = Texts("fa")


@pytest.fixture(autouse=True)
async def isolate_queue(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    yield


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session)
    panel = await factories.panel(session, shop, PanelKind.MARZBAN, inbounds=["vless:VLESS TCP"])
    fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake)
    plan = await factories.plan(session, shop, panel, price=200_000, days=30, data_gb=50)
    cust = await factories.customer(session, shop)
    cust.first_name = "Sara"
    await wallet.topup(session, shop.id, cust.id, 2_000_000, provider="card", payment_id=cust.id)
    await session.commit()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    yield {"shop": shop, "plan": plan, "cust": cust, "tg": tg}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


def test_tier_rules() -> None:
    assert growth.validate_tiers(list(reversed(TIERS)))[0]["title"] == "Silver"
    assert growth.tier_for(TIERS, 50_000) is None
    assert growth.tier_for(TIERS, 300_000)["title"] == "Gold"  # type: ignore[index]
    assert growth.next_tier(TIERS, 150_000)["title"] == "Gold"  # type: ignore[index]
    for bad in (
        [{"title": "", "min_spent": 1, "percent": 5}],
        [{"title": "x", "min_spent": 0, "percent": 80}],
        "x",
    ):
        with pytest.raises(growth.GrowthError):
            growth.validate_tiers(bad)
    assert growth.masked("Sara") == "S***a" and growth.masked("") == "***"
    assert growth.masked_id(5694312345) == "56943******"
    assert growth.masked_id(None) == "******" and growth.masked_id(42) == "******"


async def test_spending_raises_the_level_and_the_discount(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    await shop_settings.set_many(session, shop.id, {"loyalty_enabled": True, "loyalty_tiers": TIERS})
    await session.commit()
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await session.refresh(cust)
    assert cust.total_spent == 200_000 and cust.tier_percent == 5
    order = await orders.create(
        session, cust, await orders.draft(session, cust, OrderKind.PURCHASE, plan=plan)
    )
    assert order.reseller_discount == 10_000 and order.amount == 190_000 and order.params["tier"] == 5
    assert pricing.reseller_percent(cust) == 5
    await drain(session, "notify")
    assert any("Silver" in text for text in env["tg"].texts(cust.telegram_id))


async def test_wheel_pays_once_per_period(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust = env["shop"], env["cust"]
    prizes = [
        {"label": "10k", "kind": "wallet", "value": 10_000, "weight": 1},
        {"label": "-", "kind": "nothing", "value": 0, "weight": 0},
    ]
    await shop_settings.set_many(
        session, shop.id, {"wheel_enabled": True, "wheel_prizes": prizes, "wheel_buyers_only": False}
    )
    cfg = await shop_settings.load(session, shop.id)
    before = await wallet_balance(session, shop.id, cust.id)
    result = await growth.spin(session, cust, cfg)
    assert result.prize["label"] == "10k"
    assert await wallet_balance(session, shop.id, cust.id) == before + 10_000
    with pytest.raises(growth.GrowthError, match="wheel_wait"):
        await growth.spin(session, cust, cfg)
    await session.execute(update(WheelSpin).values(created_at=utcnow() - timedelta(days=2)))
    discount = [{"label": "20%", "kind": "discount", "value": 20, "weight": 5}, prizes[1]]
    await shop_settings.set_many(session, shop.id, {"wheel_prizes": discount})
    second = await growth.spin(session, cust, await shop_settings.load(session, shop.id))
    code = await session.scalar(select(DiscountCode).where(DiscountCode.code == second.code))
    assert code is not None and code.value == 20 and code.max_uses == 1
    await shop_settings.set_many(session, shop.id, {"wheel_buyers_only": True})
    stranger = await factories.customer(session, shop)
    with pytest.raises(growth.GrowthError, match="wheel_buyers_only"):
        await growth.spin(session, stranger, await shop_settings.load(session, shop.id))


async def test_lottery_by_purchases_pays_the_winner(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    start = utcnow() - timedelta(minutes=1)
    lottery = Lottery(
        shop_id=shop.id,
        title="Nowruz",
        prize="50k",
        prize_amount=50_000,
        winners=3,
        mode="purchases",
        starts_at=start,
        ends_at=utcnow() + timedelta(hours=1),
    )
    session.add(lottery)
    await session.commit()
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    assert await growth.tickets(session, lottery) == {cust.id: 1}
    with pytest.raises(growth.GrowthError, match="lottery_closed"):
        await growth.join(session, lottery, cust)  # purchase lotteries can't be joined by button
    before = await wallet_balance(session, shop.id, cust.id)
    assert await growth.draw(session, lottery) == [cust.id]
    assert await growth.draw(session, lottery) == [cust.id]  # drawing again changes nothing
    assert await wallet_balance(session, shop.id, cust.id) == before + 50_000


def test_weighted_pick_never_repeats() -> None:
    winners = growth.pick_winners({1: 5, 2: 1, 3: 0}, 5)
    assert sorted(winners) == [1, 2]


async def test_trust_channel_posts_a_masked_report(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    shop.bot_username = "pauru_test_bot"
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1001234, kinds=["purchase", "renew"]))
    await session.commit()
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    service = await session.scalar(select(Service).where(Service.customer_id == cust.id))
    assert service is not None
    service.label = "My monthly line"
    await session.commit()
    await drain(session, "notify")
    posts = env["tg"].texts(-1001234)
    assert len(posts) == 1
    report = posts[0]
    assert growth.masked_id(cust.telegram_id) in report
    assert str(cust.telegram_id) not in report and "Sara" not in report and "S***a" not in report
    assert service.panel_username not in report
    assert plan.title in report and "My monthly line" in report and money(plan.price) in report
    post = next(
        call
        for call in reversed(env["tg"].calls)
        if isinstance(call, SendMessage) and call.chat_id == -1001234
    )
    row = post.reply_markup.inline_keyboard
    assert len(row) == 1 and row[0][0].url == "https://t.me/pauru_test_bot"


async def test_trust_channels_follow_their_kinds_and_pause(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1002101, kinds=["renew"]))  # not a purchase
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1002102, kinds=["purchase"], is_active=False))
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1002103, kinds=["purchase"], sort=1))
    await session.commit()
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    await drain(session, "notify")
    assert env["tg"].texts(-1002101) == [] and env["tg"].texts(-1002102) == []
    assert len(env["tg"].texts(-1002103)) == 1


async def test_trial_and_free_orders_post_only_when_asked_for(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.provisioning import lifecycle

    shop, cust, plan = env["shop"], env["cust"], env["plan"]
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1003101, kinds=["trial"]))
    session.add(TrustChannel(shop_id=shop.id, chat_id=-1003102, kinds=["extra_days"]))
    await session.commit()
    trial = Order(
        shop_id=shop.id,
        customer_id=cust.id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.COMPLETED,
        plan_id=plan.id,
        amount=0,
        completed_at=utcnow(),
    )
    free = Order(
        shop_id=shop.id,
        customer_id=cust.id,
        kind=OrderKind.EXTRA_DAYS,
        status=OrderStatus.COMPLETED,
        plan_id=plan.id,
        amount=0,
        completed_at=utcnow(),
    )
    session.add_all([trial, free])
    await session.commit()
    for order in (trial, free):
        await lifecycle.after_sale(session, order)
        await session.commit()
    await drain(session, "notify")
    posts = env["tg"].texts(-1003101)
    assert len(posts) == 1
    assert growth.masked_id(cust.telegram_id) in posts[0]
    assert str(cust.telegram_id) not in posts[0] and plan.title in posts[0]
    assert FA("trust_trial").split("\n")[0] in posts[0]
    assert env["tg"].texts(-1003102) == []  # a free bonus is not worth a public post
