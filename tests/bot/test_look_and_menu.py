"""Premium emoji (BOT-10), start media and buy banner (BOT-11), menu modes (BOT-12), timed
broadcast pins (GRW-06) and staff notes on services (SVC-16)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage, SendPhoto, SendSticker
from aiogram.types import MessageEntity
from sqlalchemy import select, update

import paurubot.notifications.handlers  # noqa: F401  (registers the queues)
from paurubot.auth.staff import create_staff
from paurubot.bot import premium_emoji
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin, Menu
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import Broadcast, BroadcastPin, Job, JobStatus, Service, ServiceStatus, StaffRole
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from paurubot.notifications.handlers import audience_conditions
from paurubot.panels import GB
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_resellers import drain

FA = Texts("fa")
OWNER = 8201
UID = 8202


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Look")
    panel = await factories.panel(session, shop)
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="look_owner", password=None, telegram_id=OWNER
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "fake": fake, "send": send, "bot": bot}
    notify_telegram.set_bot(None)
    premium_emoji.forget()


def test_premium_emoji_text_rewrite() -> None:
    mapping = premium_emoji.clean_map({"🔥": "5368324170671202286", "<b>": "1", "😀": "nope"})
    assert mapping == {"🔥": "5368324170671202286"}
    out = premium_emoji.apply("🔥 hot <code>🔥</code> <b>🔥</b>", mapping)
    assert out.count("<tg-emoji") == 2 and "<code>🔥</code>" in out
    assert premium_emoji.apply(out, mapping) == out  # never wraps twice


async def test_premium_emoji_added_from_the_bot_and_sent(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, bot = env["send"], env["fake"], env["shop"], env["bot"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="look", action="emoji_add").pack()))
    entity = MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="5368324170671202286")
    await send(message_update(OWNER, "🔥", entities=[entity]))
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    assert cfg["premium_emoji_enabled"] and cfg["premium_emojis"] == {"🔥": "5368324170671202286"}
    bot.session.middleware(premium_emoji.PremiumEmojiMiddleware(shop.id))
    await bot.send_message(UID, "🔥 sale")
    assert '<tg-emoji emoji-id="5368324170671202286">' in fake.last(SendMessage).text


async def test_start_media_banner_and_inline_menu(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await shop_settings.set_many(
        session,
        shop.id,
        {
            "start_media": {"type": "sticker", "file_id": "stk"},
            "buy_banner_file_id": "ban",
            "menu_mode": "inline",
        },
    )
    await session.commit()
    await send(message_update(UID, "/start"))
    assert fake.last(SendSticker).sticker == "stk"
    welcome = fake.last(SendMessage)
    callbacks = [data for _, data in fake.buttons(welcome) if data]
    assert Menu(item="wallet").pack() in callbacks
    await send(callback_update(UID, Menu(item="wallet").pack()))
    assert FA("btn_topup") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(callback_update(UID, Menu(item="buy").pack()))
    assert any(isinstance(c, SendPhoto) and c.photo == "ban" for c in fake.calls)


async def test_inactive_audience_and_timed_pins(session, env) -> None:  # type: ignore[no-untyped-def]
    send, shop, panel = env["send"], env["shop"], env["panel"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(UID, "/start"))
    idle = await _customer(session, shop.id, UID)
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=idle.id,
            panel_id=panel.id,
            panel_username="u_idle",
            status=ServiceStatus.ACTIVE,
            data_limit=GB,
            online_at=utcnow() - timedelta(days=20),
        )
    )
    await session.commit()
    from sqlalchemy import func

    from paurubot.db.models import Customer

    count = await session.scalar(
        select(func.count(Customer.id)).where(*audience_conditions(shop.id, "inactive"))
    )
    assert count == 1
    await send(callback_update(OWNER, Admin(section="broadcast").pack()))
    await send(message_update(OWNER, "We miss you"))
    row = await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()))
    assert row is not None
    for _ in range(3):  # off -> until unpinned -> 24 h -> 48 h
        await send(callback_update(OWNER, Admin(section="bc", action="pin", id=row.id).pack()))
    await session.refresh(row)
    assert row.audience["pin"] and row.audience["pin_hours"] == 48
    index = ("all", "buyers", "active", "inactive", "never_bought", "resellers").index("inactive")
    await send(callback_update(OWNER, Admin(section="bc", action="aud", id=row.id, page=index).pack()))
    await send(callback_update(OWNER, Admin(section="bc", action="send", id=row.id).pack()))
    await drain(session, "broadcast")
    pin = await session.scalar(select(BroadcastPin))
    assert pin is not None and pin.chat_id == UID and pin.unpin_at is not None
    pin.unpin_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    from paurubot.worker import queue

    await queue.enqueue(session, "broadcast", "unpin", {}, shop_id=shop.id)
    await session.commit()
    await drain(session, "broadcast")
    assert await session.scalar(select(BroadcastPin)) is None


async def test_staff_note_on_a_service(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel = env["send"], env["fake"], env["shop"], env["panel"]
    await send(message_update(OWNER, "/start"))
    cust = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=cust.id,
        panel_id=panel.id,
        panel_username="u_note",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.commit()
    await send(callback_update(OWNER, Admin(section="svcnote", id=service.id).pack()))
    await send(message_update(OWNER, "paid in cash"))
    await session.refresh(service)
    assert service.staff_note == "paid in cash"
    assert FA("adm_service_note", note="paid in cash") in fake.last(SendMessage).text


async def test_quick_link_opens_its_section(session, env) -> None:  # type: ignore[no-untyped-def]
    """`t.me/<bot>?start=go_<item>` from the panel's quick links opens that part of the bot."""
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(UID, "/start go_wallet"))
    assert FA("btn_topup") in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    # A section that is off, or not a menu item at all, just shows the menu.
    await send(message_update(UID, "/start go_trial"))
    assert FA("btn_topup") not in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await send(message_update(UID, "/start go_admin"))
    assert FA("btn_topup") not in [text for text, _ in fake.buttons(fake.last(SendMessage))]
    await shop_settings.set_many(session, shop.id, {"trial_enabled": True})
    await session.commit()
    before = len(fake.calls)
    await send(message_update(UID, "/start go_trial"))
    assert len(fake.calls) > before + 1  # the menu, then the trial screen


async def test_keyword_auto_replies(session, env) -> None:  # type: ignore[no-untyped-def]
    """A free message with one of the shop's keywords gets that answer instead of the menu."""
    send, fake, shop = env["send"], env["fake"], env["shop"]
    replies = [
        {"words": ["کند", "قطع"], "text": "لینک اشتراک را یک بار به‌روز کن."},
        {"words": ["iPhone", "آیفون"], "text": "آموزش آیفون"},
    ]
    await shop_settings.set_many(session, shop.id, {"auto_replies": replies})
    await session.commit()
    await send(message_update(UID, "/start"))
    await send(message_update(UID, "سلام سرعتم خیلی کند شده"))
    assert fake.last(SendMessage).text == "لینک اشتراک را یک بار به‌روز کن."
    await send(message_update(UID, "IPHONE setup?"))
    assert fake.last(SendMessage).text == "آموزش آیفون"
    await send(message_update(UID, "چیز دیگری"))
    assert fake.last(SendMessage).text != "آموزش آیفون"  # no keyword: the menu as before
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, shop.id, {"auto_replies": [{"words": [], "text": "x"}]})


async def test_phone_asked_only_before_buying(session, env) -> None:  # type: ignore[no-untyped-def]
    """Settings › phone «before purchase»: the bot opens; a new order asks for the number."""
    from paurubot.core import orders
    from paurubot.db.models import Customer, OrderKind

    send, fake, shop = env["send"], env["fake"], env["shop"]
    await shop_settings.set_many(session, shop.id, {"require_phone": True, "phone_when": "purchase"})
    await session.commit()
    await send(message_update(UID, "/start"))
    assert fake.last(SendMessage).text != FA("phone_required")
    plan = await factories.plan(session, shop, env["panel"])
    customer = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == UID)
    )
    draft = await orders.draft(session, customer, OrderKind.PURCHASE, plan=plan)
    with pytest.raises(orders.OrderError) as caught:
        await orders.create(session, customer, draft)
    assert caught.value.key == "phone_required"
    customer.phone = "989121234567"
    await orders.create(session, customer, draft)
    with pytest.raises(shop_settings.SettingError):
        await shop_settings.set_many(session, shop.id, {"phone_when": "never"})


async def test_forced_referrer(session, env) -> None:  # type: ignore[no-untyped-def]
    """Settings › growth «invite required»: a newcomer needs an inviter's link or code first."""
    from paurubot.db.models import Customer

    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(8301, "/start"))  # an existing customer, who will invite
    await shop_settings.set_many(session, shop.id, {"referral_required": True})
    await session.commit()
    await send(message_update(8302, "/start"))
    assert fake.last(SendMessage).text == FA("referral_required")
    await send(message_update(8302, "سلام"))
    assert fake.last(SendMessage).text == FA("referral_required")
    await send(message_update(8302, "999999"))  # not a customer of this shop
    assert fake.last(SendMessage).text == FA("referral_required_invalid")
    await send(message_update(8302, "8301"))  # the inviter's code
    newcomer = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == 8302)
    )
    await session.refresh(newcomer)
    inviter = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == 8301)
    )
    assert newcomer.referrer_id == inviter.id
    await send(message_update(8302, "/start"))
    assert fake.last(SendMessage).text != FA("referral_required")
    # A link carries the code too.
    await send(message_update(8303, "/start ref_8301"))
    third = await session.scalar(
        select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == 8303)
    )
    await session.refresh(third)
    assert third.referrer_id == inviter.id and fake.last(SendMessage).text != FA("referral_required")


async def test_customer_adds_a_previous_service_from_the_bot(session, env) -> None:  # type: ignore[no-untyped-def]
    """«Add a previous service» in My services: paste a link or username; it joins or waits."""
    from paurubot.bot.callbacks import Svc
    from paurubot.db.models import Customer, ServiceClaim
    from paurubot.panels import registry
    from tests.fakes.panels import FakeTokenPanel, transport
    from tests.unit.test_importer import _panel_user

    send, fake, shop, panel = env["send"], env["fake"], env["shop"], env["panel"]
    CUID = 55507777  # a real-length Telegram id, as panel notes carry them
    panel_fake = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(panel_fake)
    try:
        _panel_user(panel_fake, "mine_old", note=f"tg:{CUID}")
        _panel_user(panel_fake, "unknown_old")
        await send(message_update(CUID, "/start"))
        await send(message_update(CUID, FA("btn_services")))
        assert Svc(action="claim").pack() not in [d for _, d in fake.buttons(fake.last(SendMessage)) if d]
        await shop_settings.set_many(session, shop.id, {"service_import_enabled": True})
        await session.commit()
        await send(message_update(CUID, FA("btn_services")))
        assert fake.last(SendMessage).text == FA("no_services")  # still offered when there are none
        assert Svc(action="claim").pack() in [d for _, d in fake.buttons(fake.last(SendMessage)) if d]
        await send(callback_update(CUID, Svc(action="claim").pack()))
        assert fake.last(SendMessage).text == FA("import_ask")
        await send(message_update(CUID, "https://sub.example.com/sub/mine_old"))
        assert fake.last(SendMessage).text == FA("import_added", name="mine_old")
        await send(callback_update(CUID, Svc(action="claim").pack()))
        await send(message_update(CUID, "unknown_old"))
        assert fake.last(SendMessage).text == FA("import_waiting")
        await send(callback_update(CUID, Svc(action="claim").pack()))
        await send(message_update(CUID, "nobody_here"))
        assert fake.last(SendMessage).text == FA("import_not_found")
        customer = await session.scalar(
            select(Customer).where(Customer.shop_id == shop.id, Customer.telegram_id == CUID)
        )
        statuses = sorted(
            await session.scalars(select(ServiceClaim.status).where(ServiceClaim.customer_id == customer.id))
        )
        assert statuses == ["approved", "pending"]
    finally:
        registry.test_transports.clear()
        registry._cache.clear()
