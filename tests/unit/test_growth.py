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
from paurubot.worker import queue
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


async def _buyer(session, shop, plan, times: int, name: str):  # type: ignore[no-untyped-def]
    buyer = await factories.customer(session, shop, username=name.lower())
    buyer.first_name = name
    await wallet.topup(session, shop.id, buyer.id, 2_000_000, provider="card", payment_id=buyer.id + 10_000)
    await session.commit()
    for _ in range(times):
        await _buy(session, buyer, OrderKind.PURCHASE, plan=plan)
    await drain(session, "provision")
    return buyer


async def test_points_lottery_ranks_buyers_and_hands_each_place_its_prize(session, env) -> None:  # type: ignore[no-untyped-def]
    """Every 100,000 spent is a point; the two best scores win: a new 10 GB, 30-day service for
    first place and 10,000 in second place's wallet. Winners hear what they won (and get the new
    config like any purchase); staff get who won what."""
    from paurubot.db.models import Staff

    shop, cust, plan, tg = env["shop"], env["cust"], env["plan"], env["tg"]
    session.add(Staff(shop_id=shop.id, telegram_id=990077, role="owner", display_name="Boss"))
    start = utcnow() - timedelta(minutes=1)
    lottery = Lottery(
        shop_id=shop.id,
        title="Azar",
        prize="",
        mode="points",
        pick="top",
        points_per=100_000,
        board="top",
        prizes=[
            {"kind": "service", "value": 10, "days": 30, "panel_id": plan.panel_id, "label": ""},
            {"kind": "wallet", "value": 10_000, "label": ""},
        ],
        winners=2,
        starts_at=start,
        ends_at=utcnow() + timedelta(hours=1),
    )
    session.add(lottery)
    await session.commit()
    await _buy(session, cust, OrderKind.PURCHASE, plan=plan)  # 200,000: 2 points
    await drain(session, "provision")
    big = await _buyer(session, shop, plan, 2, "Reza")  # 400,000: 4 points
    await factories.customer(session, shop)  # no purchase: not on the board
    await session.commit()

    board = await growth.standings(session, lottery)
    assert [(row.customer_id, row.points, row.purchases, row.spent) for row in board] == [
        (big.id, 4, 2, 400_000),
        (cust.id, 2, 1, 200_000),
    ]
    before_wallet = await wallet_balance(session, shop.id, cust.id)
    assert await growth.draw(session, lottery) == [big.id, cust.id]
    await queue.enqueue(session, "notify", "lottery_result", {"lottery_id": lottery.id}, shop_id=shop.id)
    await session.commit()
    awards = lottery.result["awards"]
    assert [(a["rank"], a["kind"], a["status"]) for a in awards] == [
        (1, "service", "queued"),
        (2, "wallet", "paid"),
    ]
    assert awards[0]["points"] == 4
    assert await wallet_balance(session, shop.id, cust.id) == before_wallet + 10_000
    prize_order = await session.get(Order, awards[0]["order_id"])
    assert prize_order is not None and prize_order.amount == 0 and prize_order.customer_id == big.id
    # the prize is not a purchase: it earns no points in a lottery of its own
    lottery_id, cust_tg, big_tg, big_id = lottery.id, cust.telegram_id, big.telegram_id, big.id

    await drain(session, "provision")  # a brand-new service for the winner
    session.expire_all()
    services = list(await session.scalars(select(Service).where(Service.customer_id == big_id)))
    assert len(services) == 3
    newest = max(services, key=lambda row: row.id)
    assert newest.data_limit == 10 * 1024**3 and newest.expire_at is not None
    assert timedelta(days=29) < newest.expire_at - utcnow() <= timedelta(days=30)

    await drain(session, "notify")
    second_texts = "\n".join(tg.texts(cust_tg))
    assert FA("lottery_won_paid") in second_texts and "Azar" in second_texts
    first_texts = "\n".join(tg.texts(big_tg))
    assert FA("lottery_won_service") in first_texts and (newest.panel_username or "") in first_texts
    staff_texts = "\n".join(tg.texts(990077))
    assert "Azar" in staff_texts and str(big_tg) in staff_texts and str(cust_tg) in staff_texts
    assert "@reza" in staff_texts and (newest.panel_username or "") in staff_texts  # made and sent
    drawn = await session.get(Lottery, lottery_id)
    assert drawn is not None


async def test_a_prize_whose_panel_is_gone_is_left_to_staff(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import Panel

    shop, plan, tg = env["shop"], env["plan"], env["tg"]
    lottery = Lottery(
        shop_id=shop.id,
        title="Join",
        prize="",
        mode="join",
        pick="random",
        prizes=[{"kind": "service", "value": 5, "days": 10, "panel_id": plan.panel_id, "label": ""}],
        winners=1,
        starts_at=utcnow() - timedelta(minutes=1),
        ends_at=utcnow() + timedelta(hours=1),
    )
    session.add(lottery)
    lonely = await factories.customer(session, shop)
    panel = await session.get(Panel, plan.panel_id)
    assert panel is not None
    panel.is_active = False
    await session.commit()
    assert await growth.join(session, lottery, lonely) is True
    assert await growth.draw(session, lottery) == [lonely.id]
    await queue.enqueue(session, "notify", "lottery_result", {"lottery_id": lottery.id}, shop_id=shop.id)
    await session.commit()
    assert lottery.result["awards"][0]["status"] == "failed"
    await drain(session, "notify")
    assert FA("lottery_won_manual") in "\n".join(tg.texts(lonely.telegram_id))


async def test_old_lotteries_still_pay_every_winner_the_same(session, env) -> None:  # type: ignore[no-untyped-def]
    lottery = Lottery(prize="50k", prize_amount=50_000, winners=3, title="x", mode="join")
    assert growth.prize_list(lottery) == [{"kind": "wallet", "value": 50_000, "label": "50k"}] * 3
    lottery.prize_amount = 0
    assert growth.prize_list(lottery)[0]["kind"] == "text"
    lottery.prize = ""
    assert growth.prize_list(lottery) == []
    for bad in (
        [{"kind": "gold", "value": 1}],
        [{"kind": "service", "value": 0, "days": 30, "panel_id": 1}],
        [{"kind": "service", "value": 10, "days": 0, "panel_id": 1}],
        [{"kind": "service", "value": 10, "days": 30}],
        [{"kind": "text", "value": 0, "label": ""}],
        [{"kind": "wallet", "value": True}],
        [{"kind": "wallet", "value": 1}] * 21,
    ):
        with pytest.raises(growth.GrowthError):
            growth.validate_lottery_prizes(bad)
