"""Buying and managing a panel admin account in the bot, through the real dispatcher (TEN-14)."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import update

import paurubot.notifications.handlers  # noqa: F401  (registers the notify queue)
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Acc, Pay
from paurubot.core import settings as shop_settings
from paurubot.core.formatting import esc
from paurubot.db.models import AccountPlan, Job, JobStatus, Order, PanelAccount, PanelKind
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import registry
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_resellers import drain

FA = Texts("fa")
UID = 7401


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Pauru Admins")
    panel = await factories.panel(session, shop, PanelKind.PASARGUARD)
    fake_panel = FakeTokenPanel("pasarguard")
    registry.test_transports[panel.id] = transport(fake_panel)
    plan = AccountPlan(
        shop_id=shop.id, panel_id=panel.id, title="Pro admin", role_id=2, data_gb=300, days=30, price=800_000
    )
    session.add(plan)
    await shop_settings.set_many(session, shop.id, {"panel_accounts_enabled": True})
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "plan": plan, "fake": fake, "panel": fake_panel, "send": send}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


async def test_buy_an_admin_account_and_get_the_login(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    await send(message_update(UID, "/start"))
    menu = fake.last(SendMessage).reply_markup
    assert FA("btn_accounts") in [b.text for row in menu.keyboard for b in row]
    customer = await _customer(session, shop.id, UID)
    await wallet.topup(session, shop.id, customer.id, 1_000_000, provider="card", payment_id=customer.id)
    await session.commit()
    await send(message_update(UID, FA("btn_accounts")))
    assert FA("acc_btn_buy") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID, Acc(action="plans").pack()))
    assert any("Pro admin" in text for text, _ in fake.buttons(fake.last(EditMessageText)))
    await send(callback_update(UID, Acc(action="buy", id=plan.id).pack()))
    checkout = fake.last(EditMessageText)
    assert "800,000" in checkout.text or "۸۰۰" in checkout.text
    pay = next(data for text, data in fake.buttons(checkout) if text == FA("btn_pay_wallet"))
    assert Pay.unpack(pay).method == "wallet"
    await send(callback_update(UID, pay))
    await drain(session, "provision")
    await drain(session, "notify")
    account = (await session.execute(PanelAccount.__table__.select())).first()
    assert account is not None
    password = esc(env["panel"].sold_admins[account.username]["password"])  # HTML, as sent
    login = next(c for c in fake.calls if isinstance(c, SendMessage) and password in (c.text or ""))
    assert account.username in login.text
    # Copyable on a phone: a protected message cannot be copied at all, and each value has a
    # button that copies exactly it.
    assert login.protect_content is not True  # left to the bot's default, which never protects
    copies = [b.copy_text.text for row in login.reply_markup.inline_keyboard for b in row if b.copy_text]
    assert copies == [account.username, env["panel"].sold_admins[account.username]["password"]]
    order = (await session.execute(Order.__table__.select().where(Order.customer_id == customer.id))).first()
    assert order is not None and order.status == "completed"
    # The account card, with its actions.
    await send(callback_update(UID, Acc(action="view", id=account.id).pack()))
    labels = [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    for key in ("acc_btn_login", "acc_btn_renew", "acc_btn_password", "acc_btn_pause"):
        assert FA(key) in labels, key


async def test_someone_else_cannot_open_the_account(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    await send(message_update(UID + 1, "/start"))
    owner = await _customer(session, shop.id, UID + 1)
    account = PanelAccount(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username="r1_ab12",
        password_enc="x",
    )
    session.add(account)
    await session.commit()
    await send(message_update(UID + 2, "/start"))
    fake.reset()
    await send(callback_update(UID + 2, Acc(action="login", id=account.id).pack()))
    assert not [c for c in fake.calls if isinstance(c, SendMessage)]


async def test_buy_extra_days_with_a_tap_or_a_typed_amount(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    plan.day_price = 4_000
    await shop_settings.set_many(
        session, shop.id, {"panel_accounts_days_min": 10, "panel_accounts_days_max": 45}
    )
    await send(message_update(UID + 3, "/start"))
    owner = await _customer(session, shop.id, UID + 3)
    from datetime import timedelta

    from paurubot.db.base import utcnow

    account = PanelAccount(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username="r3_days",
        password_enc="x",
        data_limit=0,
        expire_at=utcnow() + timedelta(days=5),
    )
    session.add(account)
    await wallet.topup(session, shop.id, owner.id, 1_000_000, provider="card", payment_id=owner.id)
    await session.commit()

    await send(callback_update(UID + 3, Acc(action="view", id=account.id).pack()))
    card = fake.last(EditMessageText)
    days = next(data for text, data in fake.buttons(card) if text == FA("acc_btn_days"))
    await send(callback_update(UID + 3, days))
    picker = fake.last(EditMessageText)
    assert FA("acc_choose_days_range", min_value=10, max_value=45) == picker.text
    taps = [
        Acc.unpack(data).value
        for _text, data in fake.buttons(picker)
        if Acc.unpack(data).action == "buy_days"
    ]
    assert taps == [15, 30]  # the bot's steps inside 10..45
    ask = next(data for text, data in fake.buttons(picker) if text == FA("acc_btn_custom_amount"))
    await send(callback_update(UID + 3, ask))
    assert fake.last(SendMessage).text == FA("acc_ask_days", min_value=10, max_value=45)
    await send(message_update(UID + 3, "50"))  # outside the range: asked again
    assert fake.last(SendMessage).text == FA("acc_amount_range", min_value=10, max_value=45)
    await send(message_update(UID + 3, "۲۰"))
    checkout = fake.last(SendMessage)
    assert FA("acc_checkout_days", value=20) in checkout.text
    order = (await session.execute(Order.__table__.select().where(Order.customer_id == owner.id))).first()
    assert order is not None and order.kind == "account_days" and order.amount == 80_000


async def test_renew_offers_the_plans_packages_and_a_custom_renewal(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    plan.renew_packages_enabled = True
    plan.renew_packages = [{"days": 30, "gb": 300, "price": 700_000}]
    plan.renew_custom_enabled = True
    plan.day_price, plan.gb_price = 10_000, 1_000
    plan.days_min, plan.days_max, plan.gb_min, plan.gb_max = 10, 60, 50, 500
    await send(message_update(UID + 4, "/start"))
    owner = await _customer(session, shop.id, UID + 4)
    from datetime import timedelta

    from paurubot.db.base import utcnow

    account = PanelAccount(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username="r4_renew",
        password_enc="x",
        data_limit=300 * 1024**3,
        expire_at=utcnow() + timedelta(days=2),
    )
    session.add(account)
    await wallet.topup(session, shop.id, owner.id, 5_000_000, provider="card", payment_id=owner.id)
    await session.commit()

    await send(callback_update(UID + 4, Acc(action="renew", id=account.id).pack()))
    menu = fake.last(EditMessageText)
    assert menu.text == FA("acc_choose_renew")
    labels = [text for text, _ in fake.buttons(menu)]
    assert FA("acc_renew_package", days=30, gb=300, price="700,000") in labels
    custom = next(data for text, data in fake.buttons(menu) if text == FA("acc_btn_renew_custom"))
    await send(callback_update(UID + 4, custom))
    assert fake.last(SendMessage).text == FA("acc_ask_renew_days", min_value=10, max_value=60, price="10,000")
    await send(message_update(UID + 4, "70"))
    assert fake.last(SendMessage).text == FA("acc_amount_range", min_value=10, max_value=60)
    await send(message_update(UID + 4, "20"))
    assert fake.last(SendMessage).text == FA("acc_ask_renew_gb", min_value=50, max_value=500, price="1,000")
    await send(message_update(UID + 4, "۲۰۰"))
    checkout = fake.last(SendMessage)
    assert FA("acc_renew_line", days=20, gb=200) in checkout.text
    order = (await session.execute(Order.__table__.select().where(Order.customer_id == owner.id))).first()
    assert order is not None and order.kind == "account_renew"
    assert order.amount == 20 * 10_000 + 200 * 1_000 and order.params["renew"] == {"days": 20, "gb": 200}


async def test_auto_renew_toggle_on_the_account_card(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    await send(message_update(UID + 5, "/start"))
    owner = await _customer(session, shop.id, UID + 5)
    account = PanelAccount(
        shop_id=shop.id,
        customer_id=owner.id,
        panel_id=plan.panel_id,
        plan_id=plan.id,
        username="r5_auto",
        password_enc="x",
    )
    session.add(account)
    await session.commit()
    await send(callback_update(UID + 5, Acc(action="view", id=account.id).pack()))
    auto = next(
        data for text, data in fake.buttons(fake.last(EditMessageText)) if text == FA("acc_btn_auto_off")
    )
    await send(callback_update(UID + 5, auto))  # one choice: switched on at once
    await session.refresh(account)
    assert account.auto_renew == {}
    assert FA("acc_btn_auto_on") in [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    await send(callback_update(UID + 5, auto))
    await session.refresh(account)
    assert account.auto_renew is None


async def test_a_discount_code_on_an_admin_account_purchase(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.db.models import DiscountCode

    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    session.add(DiscountCode(shop_id=shop.id, code="PANEL25", value=25))
    await send(message_update(UID + 6, "/start"))
    await session.commit()
    await send(callback_update(UID + 6, Acc(action="buy", id=plan.id).pack()))
    assert FA("btn_discount") not in [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    plan.discounts_allowed = True
    await session.commit()
    await send(callback_update(UID + 6, Acc(action="buy", id=plan.id).pack()))
    discount = next(
        data for text, data in fake.buttons(fake.last(EditMessageText)) if text == FA("btn_discount")
    )
    await send(callback_update(UID + 6, discount))
    assert fake.last(SendMessage).text == FA("ask_discount")
    await send(message_update(UID + 6, "PANEL25"))
    checkout = fake.last(SendMessage)
    assert FA("acc_checkout_saving", amount="200,000") in checkout.text
    owner = await _customer(session, shop.id, UID + 6)
    rows = (await session.execute(Order.__table__.select().where(Order.customer_id == owner.id))).all()
    live = [r for r in rows if r.status == "pending"]
    assert len(live) == 1 and live[0].amount == 600_000 and live[0].code_discount == 200_000


async def test_categories_come_first_when_the_shop_sorts_its_admin_plans(session, env) -> None:  # type: ignore[no-untyped-def]
    """Like config plans: Tunnel / Direct first, then that category's plans; the rest under
    "other plans"; a switched-off category hides its plans."""
    from paurubot.db.models import Category

    send, fake, shop, plan = env["send"], env["fake"], env["shop"], env["plan"]
    tunnel = Category(shop_id=shop.id, title="TUNNEL", sort=0)
    direct = Category(shop_id=shop.id, title="DIRECT", sort=1)
    hidden = Category(shop_id=shop.id, title="OFF", sort=2, is_active=False)
    session.add_all([tunnel, direct, hidden])
    await session.flush()
    plan.category_id = tunnel.id
    session.add_all(
        [
            AccountPlan(
                shop_id=shop.id,
                panel_id=plan.panel_id,
                category_id=direct.id,
                title="Direct admin",
                role_id=2,
                data_gb=100,
                days=30,
                price=300_000,
            ),
            AccountPlan(
                shop_id=shop.id,
                panel_id=plan.panel_id,
                category_id=hidden.id,
                title="Hidden admin",
                role_id=2,
                data_gb=100,
                days=30,
                price=1,
            ),
            AccountPlan(
                shop_id=shop.id,
                panel_id=plan.panel_id,
                title="Loose admin",
                role_id=2,
                data_gb=50,
                days=30,
                price=100_000,
            ),
        ]
    )
    await session.commit()
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, Acc(action="plans").pack()))
    shown = fake.last(EditMessageText)
    assert shown.text == FA("acc_choose_category")
    assert [text for text, _ in fake.buttons(shown)] == [
        "TUNNEL",
        "DIRECT",
        FA("btn_other_plans"),
        FA("btn_back"),
    ]

    await send(callback_update(UID, Acc(action="cat", id=direct.id).pack()))
    labels = [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    assert any("Direct admin" in x for x in labels) and not any("Pro admin" in x for x in labels)

    await send(callback_update(UID, Acc(action="cat", id=0).pack()))
    labels = [text for text, _ in fake.buttons(fake.last(EditMessageText))]
    assert any("Loose admin" in x for x in labels) and not any("Direct admin" in x for x in labels)

    # a plan inside a switched-off category is off sale everywhere
    await send(callback_update(UID, Acc(action="cat", id=hidden.id).pack()))
    assert not any("Hidden admin" in x for x, _ in fake.buttons(fake.last(EditMessageText)))
    await send(callback_update(UID, Acc(action="plan", id=plan.id).pack()))
    back = [data for text, data in fake.buttons(fake.last(EditMessageText)) if text == FA("btn_back")]
    assert back == [Acc(action="cat", id=tunnel.id).pack()]


async def test_without_categories_the_plans_show_straight_away(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(UID, "/start"))
    await send(callback_update(UID, Acc(action="plans").pack()))
    shown = fake.last(EditMessageText)
    assert shown.text == FA("acc_plans") and any("Pro admin" in x for x, _ in fake.buttons(shown))
