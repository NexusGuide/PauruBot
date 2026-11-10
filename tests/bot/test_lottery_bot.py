"""Lotteries in the bot (GRW-04): staff create one, give each place its prize and read the
standings; customers see the prizes, their score and place, and the masked top five."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import select, update

import paurubot.notifications.handlers  # noqa: F401  (registers the queues)
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import money
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Lottery, Order, OrderKind, OrderStatus, StaffRole
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER, BUYER, RIVAL = 61001, 62002, 63003


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Lottery shop")
    await shop_settings.set_many(session, shop.id, {"lottery_enabled": True})
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="lot_owner", password=None, telegram_id=OWNER
    )
    buyer = await factories.customer(session, shop, telegram_id=BUYER, username="sara")
    buyer.first_name = "Sara"
    rival = await factories.customer(session, shop, telegram_id=RIVAL, username="reza")
    rival.first_name = "Reza"
    await session.commit()
    shop_settings.invalidate()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "fake": fake, "send": send, "buyer": buyer, "rival": rival}
    notify_telegram.set_bot(None)
    shop_settings.invalidate()


def _paid(shop_id: int, customer_id: int, amount: int) -> Order:
    return Order(
        shop_id=shop_id,
        customer_id=customer_id,
        kind=OrderKind.PURCHASE,
        status=OrderStatus.COMPLETED,
        amount=amount,
        completed_at=utcnow(),
    )


async def test_staff_build_a_points_lottery_with_ranked_prizes(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(callback_update(OWNER, Admin(section="grw", action="lnew").pack()))
    await send(message_update(OWNER, "Azar | points | top | 30 | 10000 | top"))
    assert FA("adm_grw_lot_prize_ask", rank=FA("lot_rank", rank=1)) in fake.last(SendMessage).text
    await send(message_update(OWNER, "gold 10"))  # not a prize
    assert fake.last(SendMessage).text == FA("adm_grw_lot_prize_bad")
    await send(message_update(OWNER, FA("adm_grw_lot_kw_gb").split(",")[2] + " 10"))
    await send(message_update(OWNER, "wallet 10000"))
    lottery = await session.scalar(select(Lottery).where(Lottery.shop_id == shop.id))
    assert lottery is not None
    await session.refresh(lottery)
    assert lottery.mode == "points" and lottery.pick == "top" and lottery.points_per == 10_000
    assert lottery.board == "top" and lottery.winners == 2
    assert lottery.prizes == [
        {"kind": "gb", "value": 10, "label": ""},
        {"kind": "wallet", "value": 10_000, "label": ""},
    ]
    lottery_id = lottery.id
    # Done: the card lists both prizes by place
    await send(callback_update(OWNER, Admin(section="grw", action="lv", id=lottery_id).pack()))
    card = fake.last(EditMessageText).text
    assert FA("lot_prize_gb", value=10) in card and FA("adm_grw_pick_top") in card
    # The standings: best score first, with who they are
    session.add_all(
        [
            _paid(shop.id, env["buyer"].id, 30_000),
            _paid(shop.id, env["rival"].id, 50_000),
            _paid(shop.id, env["rival"].id, 25_000),
        ]
    )
    await session.commit()
    await send(callback_update(OWNER, Admin(section="grw", action="lbd", id=lottery_id).pack()))
    board = fake.last(EditMessageText).text
    assert board.index("Reza") < board.index("Sara")
    assert "@reza" in board and str(RIVAL) in board
    # A prize can be removed while the lottery is open
    await send(callback_update(OWNER, Admin(section="grw", action="lpdel", id=lottery_id, page=1).pack()))
    session.expire_all()
    kept = await session.get(Lottery, lottery_id)
    assert (
        kept is not None and kept.prizes == [{"kind": "gb", "value": 10, "label": ""}] and kept.winners == 1
    )


async def test_customers_see_their_place_and_the_masked_top(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    session.add(
        Lottery(
            shop_id=shop.id,
            title="Azar",
            prize="",
            mode="points",
            pick="top",
            points_per=10_000,
            board="top",
            prizes=[
                {"kind": "gb", "value": 10, "label": ""},
                {"kind": "wallet", "value": 10_000, "label": ""},
            ],
            winners=2,
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(days=1),
        )
    )
    session.add_all([_paid(shop.id, env["buyer"].id, 30_000), _paid(shop.id, env["rival"].id, 75_000)])
    await session.commit()
    await send(message_update(BUYER, FA("btn_lottery")))
    text = fake.last(SendMessage).text
    assert FA("lot_prize_gb", value=10) in text and FA("lottery_pick_top") in text
    assert FA("lottery_my_rank", rank=2, total=2) in text
    assert "R***a" in text and "Reza" not in text  # others are masked
    assert FA("lottery_by_points", per=money(10_000), points=3) in text
