"""A reseller's own bot (TEN-04)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from paurubot.auth import rbac
from paurubot.core import catalog, orders, resale
from paurubot.core import settings as shop_settings
from paurubot.db.models import (
    Job,
    JobStatus,
    OrderKind,
    OrderStatus,
    PanelKind,
    ResellerProfile,
    Service,
    Staff,
    StaffRole,
)
from paurubot.ledger import wallet
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import handlers  # noqa: F401
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import make_bot
from tests.unit.test_provisioning import drain

TOKEN = "987654321:AAH-reseller-bot-token-for-tests-only-xx"


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))

    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), "reseller_shop_bot"

    monkeypatch.setattr(resale, "bot_identity", identity)
    shop = await factories.shop(session, "Main")
    panel = await factories.panel(session, shop, PanelKind.MARZBAN)
    registry.test_transports[panel.id] = transport(FakeTokenPanel("marzban"))
    plan = await factories.plan(session, shop, panel, price=100_000, days=30, data_gb=50)
    reseller = await factories.customer(session, shop, telegram_id=61001)
    profile = ResellerProfile(
        shop_id=shop.id,
        customer_id=reseller.id,
        permissions=sorted(rbac.DEFAULT_FOR_RESELLER | {"resellers.bot"}),
        discount_override=20,
    )
    session.add(profile)
    await wallet.topup(session, shop.id, reseller.id, 200_000, provider="card", payment_id=61001)
    await session.commit()
    bot, tg = make_bot()
    notify_telegram.set_bot(bot)
    yield {"shop": shop, "plan": plan, "reseller": reseller, "profile": profile, "tg": tg}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def _sell(session, child, plan, telegram_id: int, money: int):  # type: ignore[no-untyped-def]
    buyer = await factories.customer(session, child, telegram_id=telegram_id)
    await wallet.topup(session, child.id, buyer.id, money, provider="card", payment_id=telegram_id)
    order = await orders.create(
        session, buyer, await orders.draft(session, buyer, OrderKind.PURCHASE, plan=plan)
    )
    await orders.settle_from_wallet(session, order.id)
    await session.commit()
    await drain(session, "provision")
    await session.refresh(order)
    return buyer, order


async def test_connecting_creates_a_shop_and_a_merchant(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    assert child.parent_id == env["shop"].id and child.bot_username == "reseller_shop_bot"
    assert child.bot_user_id == 987654321 and child.bot_token_enc and TOKEN not in child.bot_token_enc
    merchant = await session.scalar(select(Staff).where(Staff.shop_id == child.id))
    assert merchant is not None and merchant.role == StaffRole.MERCHANT and merchant.telegram_id == 61001
    cfg = await shop_settings.load(session, child.id, fresh=True)
    assert not cfg["trial_enabled"] and not cfg["miniapp_enabled"]
    again = await resale.connect(session, env["profile"], TOKEN, "")
    assert again.id == child.id  # reconnecting keeps the same shop
    for bad in ("nope", "1:short"):
        with pytest.raises(resale.ResaleError):
            await resale.connect(session, env["profile"], bad, "x")


async def test_reseller_bot_sells_parent_plans_at_its_own_price(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, profile = env["shop"], env["plan"], env["profile"]
    child = await resale.connect(session, profile, TOKEN, "Sara VPN")
    await session.commit()
    listed = await catalog.plans(session, child.id, None)
    assert [p.id for p in listed] == [plan.id]
    assert await resale.list_price(session, plan, child.id) == 96_000  # 80,000 after 20% off, plus 20%
    await resale.update(session, child, {"resale_prices": {str(plan.id): 120_000}})
    await session.commit()
    shop_settings.invalidate(child.id)
    assert await resale.list_price(session, plan, child.id) == 120_000

    buyer, order = await _sell(session, child, plan, 62001, 120_000)
    assert order.status == OrderStatus.COMPLETED and order.params["wholesale"] == 80_000
    service = await session.get(Service, order.service_id)
    assert service is not None and service.shop_id == child.id and service.customer_id == buyer.id
    assert await wallet_balance(session, shop.id, env["reseller"].id) == 120_000  # paid its own price
    assert await wallet_balance(session, child.id, buyer.id) == 0

    renew = await orders.draft(session, buyer, OrderKind.RENEW, plan=plan, service=service)
    assert renew.base_price == 120_000
    with pytest.raises(orders.OrderError):
        await orders.draft(session, buyer, OrderKind.EXTRA_VOLUME, service=service, extra_gb=5)


async def test_no_service_without_the_resellers_money(session, env) -> None:  # type: ignore[no-untyped-def]
    shop, plan, profile = env["shop"], env["plan"], env["profile"]
    child = await resale.connect(session, profile, TOKEN, "Sara VPN")
    await wallet.adjust(session, shop.id, env["reseller"].id, -150_000, key="drain-61001", actor="test")
    await session.commit()
    buyer, order = await _sell(session, child, plan, 62002, 96_000)
    assert order.status in {OrderStatus.FAILED, OrderStatus.REFUNDED}
    assert await session.scalar(select(Service).where(Service.customer_id == buyer.id)) is None
    assert await wallet_balance(session, child.id, buyer.id) == 96_000  # the buyer got the money back
    assert await wallet_balance(session, shop.id, env["reseller"].id) == 50_000  # untouched


async def test_stopped_or_suspended_bots_sell_nothing(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.base import utcnow

    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    buyer = await factories.customer(session, child, telegram_id=62003)
    env["profile"].suspended_at = utcnow()
    await session.flush()
    with pytest.raises(orders.OrderError, match="shop_unavailable"):
        await orders.draft(session, buyer, OrderKind.PURCHASE, plan=env["plan"])
    env["profile"].suspended_at = None
    await resale.disconnect(session, env["profile"])
    assert [s for s, _ in await resale.bots(session) if s.id == child.id] == []


async def test_editable_settings_only(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    with pytest.raises(resale.ResaleError):
        await resale.update(session, child, {"panels": []})
    with pytest.raises(resale.ResaleError):
        await resale.update(session, child, {"resale_markup_percent": 900})
    await resale.update(session, child, {"resale_markup_percent": 35, "support_username": "sara_support"})


async def test_updates_to_the_reseller_bot_land_in_its_shop(session, env) -> None:  # type: ignore[no-untyped-def]
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.methods import SendMessage

    from paurubot.bot.app import build_dispatcher
    from paurubot.db.models import Customer
    from tests.fakes.telegram import FakeSession, message_update

    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await session.commit()
    fake = FakeSession()
    child_bot = Bot(TOKEN, session=fake, default=DefaultBotProperties(parse_mode="HTML"))
    dp = build_dispatcher(MemoryStorage(), shop_id=None)
    await dp.feed_update(child_bot, message_update(63001, "/start"))
    buyer = await session.scalar(select(Customer).where(Customer.telegram_id == 63001))
    assert buyer is not None and buyer.shop_id == child.id
    assert any(isinstance(c, SendMessage) and c.chat_id == 63001 for c in fake.calls)
    menu = fake.last(SendMessage).reply_markup
    labels = [b.text for row in menu.keyboard for b in row]
    assert all(getattr(b, "web_app", None) is None for row in menu.keyboard for b in row), labels


async def test_reseller_limits_cover_its_bot_and_free_plans_are_not_resold(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    env["profile"].max_purchases_per_day = 1
    await session.commit()
    await _sell(session, child, env["plan"], 62101, 96_000)
    second = await factories.customer(session, child, telegram_id=62102)
    with pytest.raises(orders.OrderError, match="shop_unavailable"):
        await orders.draft(session, second, OrderKind.PURCHASE, plan=env["plan"])
    env["profile"].max_purchases_per_day = None
    from paurubot.db.models import Panel

    panel = await session.get(Panel, env["plan"].panel_id)
    free = await factories.plan(session, env["shop"], panel, price=0)
    await session.flush()
    with pytest.raises(orders.OrderError, match="plan_unavailable"):
        await orders.draft(session, second, OrderKind.PURCHASE, plan=free)


async def test_a_bot_stopped_by_staff_stays_stopped(session, env) -> None:  # type: ignore[no-untyped-def]
    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    await resale.disconnect(session, env["profile"], by_staff=True)
    with pytest.raises(resale.ResaleError, match="reseller_bot_blocked"):
        await resale.connect(session, env["profile"], TOKEN, "")
    await resale.allow(session, env["profile"])
    assert (await resale.connect(session, env["profile"], TOKEN, "")).id == child.id


async def test_a_stopped_bot_neither_serves_the_main_shop_nor_speaks(session, env) -> None:  # type: ignore[no-untyped-def]
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.fsm.storage.memory import MemoryStorage

    from paurubot.bot.app import build_dispatcher
    from paurubot.db.models import Customer
    from tests.fakes.telegram import FakeSession, message_update

    child = await resale.connect(session, env["profile"], TOKEN, "Sara VPN")
    buyer = await factories.customer(session, child, telegram_id=63101)
    await resale.disconnect(session, env["profile"])
    await session.commit()
    fake = FakeSession()
    stale = Bot(TOKEN, session=fake, default=DefaultBotProperties(parse_mode="HTML"))
    await build_dispatcher(MemoryStorage(), shop_id=None).feed_update(stale, message_update(63102, "/start"))
    assert await session.scalar(select(Customer).where(Customer.telegram_id == 63102)) is None
    assert fake.calls == []
    notify_telegram.set_bot(None)
    try:
        assert await notify_telegram.bot_for(child.id) is None
        await notify_telegram.send_text(buyer.telegram_id, "hello", shop_id=child.id)  # silently skipped
    finally:
        notify_telegram.forget_shop_bot(child.id)
