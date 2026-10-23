"""BOT-12: mode-aware menus and inline navigation without nested update transactions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
)

from paurubot import kv
from paurubot.bot import context, menu
from paurubot.bot.callbacks import Admin, Lang, Menu
from paurubot.bot.context import GateMiddleware, StaffIdentity
from paurubot.bot.routers import start
from paurubot.bot.utils import Btn, StaffOnly
from paurubot.config import get_settings
from paurubot.core.settings import defaults
from paurubot.db.models import StaffRole
from paurubot.i18n import Texts

USER_ID = 610001


@pytest.fixture
async def redis_clean():  # type: ignore[no-untyped-def]
    """This suite uses MemoryStorage; context counters use an isolated fake Redis."""
    from fakeredis.aioredis import FakeRedis

    client = FakeRedis(decode_responses=True)
    kv.set_redis(client)
    yield
    await kv.close_redis()


@pytest.fixture
async def telegram(monkeypatch):  # type: ignore[no-untyped-def]
    requests = []

    async def request(bot, method, request_timeout=None):  # type: ignore[no-untyped-def]
        requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(requests),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            ).as_(bot)
        return True

    monkeypatch.setattr(Bot, "__call__", request)
    bot = Bot("123456:TEST-token-for-unit-tests-only-xxxxxxxx")
    monkeypatch.setattr(get_settings(), "public_url", "https://bot.example.com/")
    yield bot, requests
    await bot.session.close()


def incoming(bot: Bot, text: str = "/start") -> Message:
    return Message(
        message_id=17,
        date=datetime.now(UTC),
        chat=Chat(id=USER_ID, type="private"),
        from_user=User(id=USER_ID, is_bot=False, first_name="Test"),
        text=text,
    ).as_(bot)


def callback(bot: Bot, item: str = "wallet", **overrides) -> CallbackQuery:  # type: ignore[no-untyped-def]
    values = dict(
        id="menu-test",
        from_user=incoming(bot).from_user,
        chat_instance="private-test",
        message=incoming(bot),
        data=Menu(item=item).pack(),
    )
    values.update(overrides)
    return CallbackQuery(**values).as_(bot)


def identity() -> StaffIdentity:
    return StaffIdentity(StaffRole.OWNER, None, frozenset({"settings.write"}))


def cfg_for(mode: str = "inline") -> dict:
    return {**defaults(), "menu_mode": mode}


@pytest.mark.parametrize("locale", ["fa", "en"])
@pytest.mark.parametrize("is_staff", [False, True])
@pytest.mark.parametrize("mode", ["reply", "inline", "miniapp"])
async def test_home_uses_selected_mode_for_customer_and_staff(telegram, locale, is_staff, mode):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t = Texts(locale)
    staff = identity() if is_staff else None
    cfg = cfg_for(mode)
    await start.home(incoming(bot), t, cfg, staff)
    messages = [r for r in requests if isinstance(r, SendMessage)]
    expected = ReplyKeyboardMarkup if mode == "reply" else InlineKeyboardMarkup
    assert isinstance(messages[-1].reply_markup, expected)
    assert messages[-1].text == t("welcome")
    # Every existing menu() completion call must return a usable menu, not Remove.
    assert isinstance(start.menu(t, cfg, staff), expected)
    if mode == "reply":
        assert len(messages) == 1
        texts = [b.text for row in messages[-1].reply_markup.keyboard for b in row]
        assert (t("btn_admin") in texts) == is_staff
    else:
        assert isinstance(messages[0].reply_markup, ReplyKeyboardRemove)
        assert any(isinstance(r, DeleteMessage) for r in requests)
        buttons = [b for row in messages[-1].reply_markup.inline_keyboard for b in row]
        assert any(b.callback_data == Admin(section="home").pack() for b in buttons) == is_staff
        if mode == "miniapp":
            assert len(buttons) == 1 + int(is_staff)
            assert buttons[0].web_app.url == "https://bot.example.com/app/"
            assert not any((b.callback_data or "").startswith("mn:") for b in buttons)


@pytest.mark.parametrize("mode", ["inline", "miniapp"])
async def test_home_survives_cosmetic_keyboard_cleanup_failure(telegram, monkeypatch, mode):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    original = Message.delete
    monkeypatch.setattr(
        Message, "delete", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("no delete"))
    )
    await start.home(incoming(bot), Texts("en"), cfg_for(mode), None)
    assert isinstance(requests[-1], SendMessage)
    assert isinstance(requests[-1].reply_markup, InlineKeyboardMarkup)
    monkeypatch.setattr(Message, "delete", original)


@pytest.mark.parametrize(
    "miniapp_enabled,public_url", [(False, "https://bot.example.com"), (True, "http://localhost")]
)
def test_unavailable_miniapp_falls_back_to_inline(monkeypatch, miniapp_enabled, public_url):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", public_url)
    cfg = {**cfg_for("miniapp"), "miniapp_enabled": miniapp_enabled}
    assert menu.menu_mode(cfg) == "inline"
    kb = start.menu(Texts("en"), cfg, None)
    assert isinstance(kb, InlineKeyboardMarkup)
    assert all(b.web_app is None for row in kb.inline_keyboard for b in row)
    assert any(b.callback_data == Menu(item="buy").pack() for row in kb.inline_keyboard for b in row)


def test_inline_layout_styles_and_optional_features(telegram):  # type: ignore[no-untyped-def]
    t = Texts("fa")
    cfg = {
        **cfg_for(),
        "trial_enabled": False,
        "menu_layout": [["services", "buy"], ["trial", "wallet"], ["miniapp"]],
        "button_styles": {"buy": {"style": "success", "emoji_id": "5368324170671202286"}},
    }
    kb = menu.inline_menu(t, cfg)
    assert [[b.text for b in row] for row in kb.inline_keyboard] == [
        [t("btn_services"), t("btn_buy")],
        [t("btn_wallet")],
        [t("btn_miniapp")],
    ]
    buy = kb.inline_keyboard[0][1]
    assert buy.style == "success" and buy.icon_custom_emoji_id == "5368324170671202286"
    assert kb.inline_keyboard[-1][0].web_app.url == "https://bot.example.com/app/"


class ExistingContext(BaseMiddleware):
    """A second update entry would deadlock on the current customer DB row in production."""

    def __init__(self, values):  # type: ignore[no-untyped-def]
        self.values = values
        self.entries = 0
        self.completions = 0

    async def __call__(self, handler, event, data):  # type: ignore[no-untyped-def]
        self.entries += 1
        assert self.entries == 1, "inline navigation re-entered update/session middleware"
        data.update(self.values)
        result = await handler(event, data)
        self.completions += 1
        return result


def routing(values):  # type: ignore[no-untyped-def]
    dp = Dispatcher(storage=MemoryStorage())
    ctx = ExistingContext(values)
    dp.update.outer_middleware(ctx)
    router = Router()
    router.callback_query.register(start.menu_press, Menu.filter())
    dp.include_router(router)
    return dp, router, ctx


@pytest.mark.parametrize("locale", ["fa", "en"])
@pytest.mark.parametrize(
    "item",
    [
        "buy",
        "services",
        "wallet",
        "trial",
        "referral",
        "reseller",
        "accounts",
        "tickets",
        "wheel",
        "lottery",
        "apps",
        "language",
        "guide",
    ],
)
async def test_inline_routes_once_with_same_session_tenant_bot_and_cleared_state(telegram, locale, item):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    cfg = {
        **cfg_for(),
        "trial_enabled": True,
        "referral_enabled": True,
        "panel_accounts_enabled": True,
        "wheel_enabled": True,
        "lottery_enabled": True,
        "download_apps": [{"name": "client"}],
    }
    t = Texts(locale, {locale: {menu.BUTTONS[item]: "custom-label-" + item}})
    session, shop, customer = object(), object(), object()
    values = dict(t=t, cfg=cfg, staff=None, session=session, shop=shop, customer=customer)
    dp, router, ctx = routing(values)
    observed = []

    async def pending_input(message):  # type: ignore[no-untyped-def]
        pytest.fail("main menu was consumed as free-form FSM input")

    async def target(message, state, **data):  # type: ignore[no-untyped-def]
        assert data["session"] is session and data["shop"] is shop and data["customer"] is customer
        assert data["bot"] is bot and message.bot is bot
        assert message.from_user.id == USER_ID and message.chat.id == USER_ID
        assert data["raw_state"] is None and await state.get_state() is None
        assert await state.get_data() == {}
        assert data["event_update"].message == message
        assert "callback_data" not in data
        observed.append(message.text)
        await message.answer("routed")

    router.message.register(pending_input, StateFilter("ticket:body"), F.text)
    router.message.register(target, Btn(menu.BUTTONS[item]))
    state = dp.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
    await state.set_state("ticket:body")
    await state.set_data({"ticket_id": 999})
    await asyncio.wait_for(dp.feed_update(bot, Update(update_id=817, callback_query=callback(bot, item))), 2)
    assert observed == [t(menu.BUTTONS[item])]
    assert ctx.entries == ctx.completions == 1
    assert sum(isinstance(r, AnswerCallbackQuery) for r in requests) == 1
    assert requests[-1].text == "routed"
    await dp.storage.close()


@pytest.mark.parametrize("item", ["trial", "referral", "accounts", "wheel", "lottery", "apps", "ghost"])
async def test_disabled_or_invalid_inline_action_does_not_dispatch(telegram, item):  # type: ignore[no-untyped-def]
    bot, _ = telegram
    cfg = cfg_for()
    cfg.update(trial_enabled=False, referral_enabled=False)
    dispatcher = SimpleNamespace(propagate_event=AsyncMock())
    state = AsyncMock()
    await start.menu_press(callback(bot, item), Menu(item=item), Texts("en"), cfg, dispatcher, bot, state)
    dispatcher.propagate_event.assert_not_awaited()
    state.clear.assert_not_awaited()


async def test_hidden_layout_action_does_not_dispatch(telegram):  # type: ignore[no-untyped-def]
    bot, _ = telegram
    cfg = {**cfg_for(), "menu_layout": [["buy", "services"]]}
    dispatcher = SimpleNamespace(propagate_event=AsyncMock())
    await start.menu_press(callback(bot), Menu(item="wallet"), Texts("en"), cfg, dispatcher, bot, AsyncMock())
    dispatcher.propagate_event.assert_not_awaited()


@pytest.mark.parametrize("kind", ["group", "inaccessible", "other-user", "no-message"])
async def test_inline_action_requires_accessible_private_chat(telegram, kind):  # type: ignore[no-untyped-def]
    bot, _ = telegram
    msg = incoming(bot)
    if kind == "group":
        msg = msg.model_copy(update={"chat": Chat(id=-10001, type="supergroup")})
    elif kind == "other-user":
        msg = msg.model_copy(update={"chat": Chat(id=USER_ID + 1, type="private")})
    elif kind == "inaccessible":
        from aiogram.types import InaccessibleMessage

        msg = InaccessibleMessage(chat=Chat(id=USER_ID, type="private"), message_id=1, date=0)
    else:
        msg = None
    dispatcher = SimpleNamespace(propagate_event=AsyncMock())
    await start.menu_press(
        callback(bot, message=msg), Menu(item="wallet"), Texts("en"), cfg_for(), dispatcher, bot, AsyncMock()
    )
    dispatcher.propagate_event.assert_not_awaited()


async def test_old_inline_menu_in_miniapp_mode_shows_only_webapp(telegram):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    dispatcher = SimpleNamespace(propagate_event=AsyncMock())
    state = AsyncMock()
    await start.menu_press(
        callback(bot), Menu(item="wallet"), Texts("en"), cfg_for("miniapp"), dispatcher, bot, state
    )
    state.clear.assert_awaited_once()
    dispatcher.propagate_event.assert_not_awaited()
    kb = requests[-1].reply_markup
    assert len(kb.inline_keyboard) == 1 and kb.inline_keyboard[0][0].web_app is not None


@pytest.mark.parametrize("block", ["channel", "phone", "terms"])
async def test_inline_actions_still_pass_security_gates(telegram, monkeypatch, block):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    cfg = {
        **cfg_for(),
        "require_phone": block == "phone",
        "terms_required": block == "terms",
        "terms_version": 2,
    }
    customer = SimpleNamespace(phone=None, terms_accepted_version=0)
    missing = [SimpleNamespace(chat_id=-123, title="Join", url="https://t.me/example")]
    monkeypatch.setattr(
        context, "missing_channels", AsyncMock(return_value=missing if block == "channel" else [])
    )
    dp, router, ctx = routing(dict(t=Texts("en"), cfg=cfg, staff=None, customer=customer, session=object()))
    dp.callback_query.middleware(GateMiddleware())
    target = AsyncMock()

    async def guarded_target(message):
        await target(message)

    router.message.register(guarded_target)
    await dp.feed_update(bot, Update(update_id=818, callback_query=callback(bot)))
    target.assert_not_awaited()
    assert ctx.entries == 1
    assert any(isinstance(r, AnswerCallbackQuery) and r.show_alert for r in requests)


@pytest.mark.parametrize("mode", ["inline", "miniapp"])
@pytest.mark.parametrize("flow", ["cancel", "joined", "terms", "language", "phone"])
async def test_return_paths_restore_selected_menu(telegram, monkeypatch, mode, flow):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    cfg = cfg_for(mode)
    t = Texts("en")
    customer = SimpleNamespace(telegram_id=USER_ID, terms_accepted_version=0, locale="en", phone=None)
    state = AsyncMock()
    call = callback(bot)
    if flow == "cancel":
        await start.cancel(incoming(bot), t, cfg, None, state)
    elif flow == "joined":
        monkeypatch.setattr(start, "missing_channels", AsyncMock(return_value=[]))
        await start.joined(call, customer, t, cfg, None, bot, AsyncMock())
    elif flow == "terms":
        await start.accept_terms(call, customer, t, cfg, None)
        assert customer.terms_accepted_version == cfg["terms_version"]
    elif flow == "language":
        await start.set_language(call, Lang(code="fa"), AsyncMock(), object(), customer, cfg, None, t)
        assert customer.locale == "fa"
    else:
        from aiogram.types import Contact

        msg = incoming(bot).model_copy(
            update={"contact": Contact(phone_number="+989121234567", first_name="Test", user_id=USER_ID)}
        )
        await start.contact(msg, customer, t, cfg, None, state)
        assert customer.phone == "989121234567"
    assert isinstance(requests[-1], SendMessage)
    assert isinstance(requests[-1].reply_markup, InlineKeyboardMarkup)
    assert any(
        isinstance(r, SendMessage) and isinstance(r.reply_markup, ReplyKeyboardRemove) for r in requests
    )
    if mode == "miniapp":
        assert len(requests[-1].reply_markup.inline_keyboard) == 1
        assert requests[-1].reply_markup.inline_keyboard[0][0].web_app is not None


@pytest.mark.parametrize("is_staff", [False, True])
async def test_inline_admin_shortcut_stays_permission_guarded(telegram, is_staff):  # type: ignore[no-untyped-def]
    bot, _ = telegram
    staff = identity() if is_staff else None
    dp, router, _ = routing(dict(t=Texts("en"), cfg=cfg_for(), staff=staff))
    called = []

    async def admin_home(call, staff):  # type: ignore[no-untyped-def]
        called.append(staff)

    router.callback_query.register(admin_home, Admin.filter(F.section == "home"), StaffOnly())
    await dp.feed_update(
        bot, Update(update_id=819, callback_query=callback(bot, data=Admin(section="home").pack()))
    )
    assert called == ([staff] if is_staff else [])


@pytest.fixture
async def menu_engine(engine):  # type: ignore[no-untyped-def]
    """Independent committed sessions on a disposable, fully migrated schema.

    Deleting the shop cascades into immutable ledger rows. Drop only this test's
    schema instead; keep every production ledger trigger enabled throughout.
    """
    from uuid import uuid4

    from alembic import command
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from paurubot.db.migrate import alembic_config

    schema = "menu_lock_" + uuid4().hex
    isolated = create_async_engine(engine.url, connect_args={"server_settings": {"search_path": schema}})

    def migrate(connection):  # type: ignore[no-untyped-def]
        cfg = alembic_config()
        cfg.attributes["connection"] = connection
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, "head")

    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with isolated.begin() as connection:
            await connection.run_sync(migrate)
        yield isolated
    finally:
        await isolated.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


async def test_postgres_inline_callback_does_not_open_a_second_customer_transaction(
    menu_engine, telegram, monkeypatch
):  # type: ignore[no-untyped-def]
    """Real customer upsert/row lock, not a mock: old nested feed_update times out."""
    from sqlalchemy import delete, select
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from paurubot.core import customers
    from paurubot.core import settings as shop_settings
    from paurubot.db.models import Customer, LedgerJournal, Shop

    bot, requests = telegram
    maker = async_sessionmaker(menu_engine, expire_on_commit=False)
    async with maker() as session:
        shop = Shop(title="Inline menu row lock regression")
        session.add(shop)
        await session.flush()
        await shop_settings.set_many(session, shop.id, {"menu_mode": "inline"})
        customer, _ = await customers.upsert_from_telegram(
            session, shop.id, USER_ID, username=None, first_name="Test", locale="en"
        )
        customer_id, shop_id = customer.id, shop.id
        await session.commit()

    monkeypatch.setattr(context, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(context, "_shop_of_bot", AsyncMock(return_value=shop_id))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(context.ContextMiddleware())
    router = Router()
    router.callback_query.register(start.menu_press, Menu.filter())
    called = []

    async def wallet_target(message, session, customer, shop):  # type: ignore[no-untyped-def]
        assert customer.id == customer_id and shop.id == shop_id
        assert await session.scalar(select(Customer.id).where(Customer.id == customer_id)) == customer_id
        called.append(customer.id)
        await message.answer("wallet opened")

    router.message.register(wallet_target, Btn("btn_wallet"))
    dp.include_router(router)
    try:
        await asyncio.wait_for(dp.feed_update(bot, Update(update_id=820, callback_query=callback(bot))), 5)
        assert called == [customer_id]
        assert requests[-1].text == "wallet opened"
        async with maker() as session:
            with pytest.raises(DBAPIError, match="ledger rows are append-only"):
                await session.execute(delete(LedgerJournal))
            await session.rollback()
    finally:
        shop_settings.invalidate(shop_id)
        await dp.storage.close()


@pytest.mark.parametrize(
    "item,handler,text_key",
    [("guide", start.guide, "guide_text"), ("language", start.language, "choose_language")],
)
async def test_inline_dispatches_real_start_handlers(telegram, item, handler, text_key):  # type: ignore[no-untyped-def]
    bot, requests = telegram
    t = Texts("fa")
    dp, router, ctx = routing(dict(t=t, cfg=cfg_for(), staff=None))
    router.message.register(handler, Btn(menu.BUTTONS[item]))
    await dp.feed_update(bot, Update(update_id=821, callback_query=callback(bot, item)))
    assert ctx.entries == ctx.completions == 1
    assert requests[-1].text == t(text_key)
