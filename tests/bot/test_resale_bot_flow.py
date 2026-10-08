"""A reseller's sales bot built and run from Telegram (TEN-17), through the real dispatcher."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage

from paurubot.auth import rbac
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin, Rb, Rs
from paurubot.core import resale
from paurubot.core import settings as shop_settings
from paurubot.db.models import ResellerProfile, ShopStatus, StaffRole
from paurubot.i18n import Texts
from paurubot.ledger import wallet
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER = 7801
RESELLER = 7802
TOKEN = "887766554:AAH-reseller-bot-token-for-flow-tests-x"
OTHER_TOKEN = "887766555:AAH-another-bot-token-for-flow-tests-y"


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    async def identity(token: str) -> tuple[int, str]:
        return int(token.split(":")[0]), f"sara_{token[:3]}_bot"

    async def no_webhook(shop_id: int) -> None:
        return None

    monkeypatch.setattr(resale, "bot_identity", identity)
    monkeypatch.setattr(resale, "point_webhook", no_webhook)
    shop = await factories.shop(session, "Resale flow")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, price=100_000, title="Monthly")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="rb_owner", password=None, telegram_id=OWNER
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "plan": plan, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


async def _reseller(session, env, rights: set[str] | None = None) -> ResellerProfile:  # type: ignore[no-untyped-def]
    await env["send"](message_update(RESELLER, "/start"))
    customer = await _customer(session, env["shop"].id, RESELLER)
    profile = ResellerProfile(
        shop_id=env["shop"].id,
        customer_id=customer.id,
        permissions=sorted(rbac.DEFAULT_FOR_RESELLER | (rights or set())),
        discount_override=20,
    )
    session.add(profile)
    await wallet.topup(session, env["shop"].id, customer.id, 300_000, provider="card", payment_id=RESELLER)
    await session.commit()
    return profile


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [text for text, _ in fake.buttons(fake.last(kind))]


def alerts(fake) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.text for c in fake.calls if isinstance(c, AnswerCallbackQuery) and c.text]


async def test_reseller_builds_and_runs_its_bot_from_telegram(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, plan = env["send"], env["fake"], env["plan"]
    profile = await _reseller(session, env, {"resellers.bot"})
    await send(message_update(RESELLER, FA("btn_reseller")))
    assert FA("rb_btn_hub") in labels(fake, SendMessage)

    await send(callback_update(RESELLER, Rb(action="home").pack()))
    assert FA("rb_btn_connect") in labels(fake)
    await send(callback_update(RESELLER, Rb(action="token").pack()))
    await send(message_update(RESELLER, "not-a-token"))
    assert FA("rb_err_bot_token_invalid") in fake.texts(RESELLER)
    fake.reset()
    await send(message_update(RESELLER, TOKEN))
    assert any(isinstance(c, DeleteMessage) for c in fake.calls)  # the token leaves the chat
    child = await resale.shop_of(session, profile)
    assert child is not None and child.bot_username == "sara_887_bot" and child.status == ShopStatus.ACTIVE
    assert FA("rb_connected", bot="sara_887_bot") in fake.texts(RESELLER)
    home = fake.last(SendMessage)
    assert "sara_887_bot" in home.text and FA("rb_btn_cards") in [t for t, _ in fake.buttons(home)]

    # Cards: a bad number is refused, a good one is saved and turns card payments on.
    await send(callback_update(RESELLER, Rb(action="card_add").pack()))
    await send(message_update(RESELLER, "1234 Sara"))
    assert FA("rb_card_invalid") in fake.texts(RESELLER)
    await send(message_update(RESELLER, "6037991234567893 Sara Ahmadi"))
    cfg = await shop_settings.load(session, child.id, fresh=True)
    assert cfg["card_accounts"] == [{"number": "6037991234567893", "holder": "Sara Ahmadi"}]
    assert cfg["card_enabled"] is True

    # Markup, support and a fixed price for one plan.
    await send(callback_update(RESELLER, Rb(action="markup").pack()))
    await send(message_update(RESELLER, "900"))
    assert FA("rb_markup_invalid", max=500) in fake.texts(RESELLER)
    await send(message_update(RESELLER, "30"))
    await send(callback_update(RESELLER, Rb(action="support").pack()))
    await send(message_update(RESELLER, "sara_help"))
    await send(callback_update(RESELLER, Rb(action="prices").pack()))
    assert any("Monthly" in text for text in labels(fake))
    await send(callback_update(RESELLER, Rb(action="price", id=plan.id).pack()))
    await send(message_update(RESELLER, "150000"))
    cfg = await shop_settings.load(session, child.id, fresh=True)
    assert cfg["resale_markup_percent"] == 30 and cfg["support_username"] == "@sara_help"
    assert cfg["resale_prices"] == {str(plan.id): 150_000}
    shop_settings.invalidate(child.id)
    assert await resale.list_price(session, plan, child.id) == 150_000
    await send(callback_update(RESELLER, Rb(action="price", id=plan.id).pack()))
    await send(message_update(RESELLER, "0"))  # back to the markup: 80,000 + 30%
    shop_settings.invalidate(child.id)
    assert await resale.list_price(session, plan, child.id) == 104_000

    # Off and on, then removed; customers stay for a later reconnect.
    await send(callback_update(RESELLER, Rb(action="toggle").pack()))
    assert (await shop_settings.load(session, child.id, fresh=True))["bot_enabled"] is False
    await send(callback_update(RESELLER, Rb(action="delete").pack()))
    assert FA("rb_btn_delete_yes") in labels(fake)
    await send(callback_update(RESELLER, Rb(action="delete_go").pack()))
    await session.refresh(child)
    assert child.bot_token_enc is None and child.status == ShopStatus.SUSPENDED


async def test_without_the_right_the_bot_screen_is_closed(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    profile = await _reseller(session, env)
    await send(message_update(RESELLER, FA("btn_reseller")))
    assert FA("rb_btn_hub") not in labels(fake, SendMessage)
    await send(callback_update(RESELLER, Rb(action="token").pack()))
    assert FA("rb_not_allowed") in alerts(fake)
    # A shop open to every reseller lets it in; connecting writes the right down for the web.
    await shop_settings.set_many(session, shop.id, {"reseller_bot_open": True})
    await session.commit()
    shop_settings.invalidate(shop.id)
    await send(callback_update(RESELLER, Rs(action="home").pack()))
    assert FA("rb_btn_hub") in labels(fake)
    await send(callback_update(RESELLER, Rb(action="token").pack()))
    await send(message_update(RESELLER, TOKEN))
    await session.refresh(profile)
    assert "resellers.bot" in profile.permissions


async def test_usage_billing_cannot_connect(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    profile = await _reseller(session, env, {"resellers.bot"})
    profile.billing_mode = "usage"
    await session.commit()
    await send(callback_update(RESELLER, Rb(action="home").pack()))
    assert FA("rb_needs_prepaid") in fake.last(EditMessageText).text
    assert FA("rb_btn_connect") not in labels(fake)
    await send(callback_update(RESELLER, Rb(action="token").pack()))
    assert FA("rb_needs_prepaid") in alerts(fake)


async def test_owner_builds_stops_and_allows_a_resellers_bot(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    profile = await _reseller(session, env)
    customer_id = profile.customer_id
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="user", id=customer_id).pack()))
    assert FA("adm_user_btn_rbot") in labels(fake)
    await send(callback_update(OWNER, Admin(section="rbot", id=customer_id).pack()))
    assert FA("adm_rbot_btn_build") in labels(fake)
    await send(callback_update(OWNER, Admin(section="rbot", action="token", id=customer_id).pack()))
    fake.reset()
    await send(message_update(OWNER, TOKEN))
    await session.refresh(profile)
    assert "resellers.bot" in profile.permissions  # the reseller runs it from now on
    notice = [c for c in fake.calls if isinstance(c, SendMessage) and c.chat_id == RESELLER]
    assert notice and "sara_887_bot" in notice[0].text
    child = await resale.shop_of(session, profile)
    assert child is not None and child.bot_token_enc is not None

    await send(callback_update(OWNER, Admin(section="rbot", action="stop", id=customer_id).pack()))
    await session.refresh(child)
    assert child.bot_token_enc is None
    assert FA("adm_rbot_btn_allow") in labels(fake)
    # Stopped by staff: the reseller can't simply reconnect.
    await send(callback_update(RESELLER, Rb(action="home").pack()))
    assert FA("rb_blocked") in fake.last(EditMessageText).text
    await send(callback_update(OWNER, Admin(section="rbot", action="allow", id=customer_id).pack()))
    assert not (await shop_settings.load(session, child.id, fresh=True)).get("resale_blocked")
    await send(callback_update(OWNER, Admin(section="rbot", action="right", id=customer_id).pack()))
    await session.refresh(profile)
    assert "resellers.bot" not in profile.permissions


async def test_merchant_manages_from_its_own_bot(session, env) -> None:  # type: ignore[no-untyped-def]
    profile = await _reseller(session, env, {"resellers.bot"})
    child = await resale.connect(session, profile, TOKEN, "Sara VPN")
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=child.id)
    await dp.feed_update(bot, message_update(RESELLER, "/start"))
    await dp.feed_update(bot, message_update(RESELLER, FA("btn_admin")))
    assert FA("rb_btn_hub") in labels(fake, SendMessage)
    await dp.feed_update(bot, callback_update(RESELLER, Rb(action="home").pack()))
    assert "sara_887_bot" in fake.last(EditMessageText).text
    # A buyer of that bot is not its merchant.
    await dp.feed_update(bot, message_update(7899, "/start"))
    fake.reset()
    await dp.feed_update(bot, callback_update(7899, Rb(action="home").pack()))
    assert FA("rb_not_allowed") in alerts(fake)
    build_dispatcher(MemoryStorage(), shop_id=env["shop"].id)


def test_card_parsing() -> None:
    persian = "\u06f6\u06f0\u06f3\u06f7" + "991234567893"
    assert resale.parse_card(persian + " Sara") == {"number": "6037991234567893", "holder": "Sara"}
    spaced = resale.parse_card("6037-9912 3456 7893   Sara  Ahmadi")
    assert spaced == {"number": "6037991234567893", "holder": "Sara Ahmadi"}
    iban = "IR" + "0" * 24
    assert resale.parse_card(iban.lower())["number"] == iban
    for bad in ("", "6037991234567890 x", "12345", "Sara 6037991234567893"):
        with pytest.raises(resale.ResaleError):
            resale.parse_card(bad)
