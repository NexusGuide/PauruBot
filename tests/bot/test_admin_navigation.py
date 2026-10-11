"""Admin landing keyboard: layout, role filtering, safe shortcuts and dispatcher routes."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import ReplyKeyboardMarkup

from paurubot.auth import rbac
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.context import StaffIdentity
from paurubot.bot.routers import admin_navigation as nav
from paurubot.bot.routers.admin_ops import ops_buttons
from paurubot.bot.routers.admin_tools import tools_kb
from paurubot.bot.states import AdminStates
from paurubot.config import get_settings
from paurubot.db.models import StaffRole
from paurubot.i18n import Texts, catalog, fields
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER = 7811
SUPPORT = 7812
CUSTOMER = 7813


def identity(role: StaffRole) -> StaffIdentity:
    return StaffIdentity(role, None, frozenset(rbac.permissions_for(role)))


def reply_labels(call):  # type: ignore[no-untyped-def]
    assert isinstance(call.reply_markup, ReplyKeyboardMarkup)
    return [b.text for row in call.reply_markup.keyboard for b in row]


def test_owner_layout_matches_reference() -> None:
    markup = nav.menu(FA, {}, identity(StaffRole.OWNER))
    expected = [[FA(f"adm_nav_{g}") for g in row] for row in nav.HOME_ROWS]
    expected.append([FA("adm_nav_back")])
    assert [[b.text for b in row] for row in markup.keyboard] == expected
    assert markup.resize_keyboard and markup.is_persistent
    assert [len(row) for row in markup.keyboard] == [1, 1, 2, 1, 2, 1, 2, 2, 2, 1]


@pytest.mark.parametrize("role", list(StaffRole))
def test_roles_never_get_empty_rows_or_unauthorized_actions(role: StaffRole) -> None:
    staff = identity(role)
    markup = nav.menu(FA, {}, staff)
    assert all(markup.keyboard)
    for group in nav.GROUPS:
        if nav.allowed(group, staff):
            for item in nav.entries(group, staff):
                assert item.permission is None or staff.can(item.permission)
    support = nav.menu(FA, {}, identity(StaffRole.SUPPORT))
    labels = [b.text for row in support.keyboard for b in row]
    assert FA("adm_nav_settings") not in labels
    assert FA("adm_nav_updates") not in labels
    assert FA("adm_nav_addpanel") not in labels
    assert FA("adm_nav_users") in labels


def test_all_previous_home_tools_are_still_reachable() -> None:
    staff = identity(StaffRole.OWNER)
    sections = {item.section for group in nav.GROUPS for item in nav.entries(group, staff)}
    previous = [*tools_kb(FA, staff), *ops_buttons(FA, staff)]
    assert {target.section for _, target in previous} <= sections
    assert {"stats", "payments", "tickets", "applications", "lookup", "broadcast", "maintenance"} <= sections


def test_locale_keys_and_placeholders_match() -> None:
    fa, en = catalog("fa"), catalog("en")
    keys = {key for key in fa if key.startswith("adm_nav_")}
    assert keys == {key for key in en if key.startswith("adm_nav_")}
    keys |= {item.key for items in nav.GROUPS.values() for item in items}
    for key in keys:
        assert key in fa and key in en
        assert fields(fa[key]) == fields(en[key])
    labels = [FA(key) for key in nav.LABEL_KEYS]
    assert len(labels) == len(set(labels))


def test_web_shortcuts_are_navigation_not_operations(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    staff = identity(StaffRole.OWNER)
    monkeypatch.setattr(get_settings(), "public_url", "https://shop.example/")
    for group, page in (("addpanel", "panels"), ("updates", "updates"), ("guide", "help")):
        kb = nav.group_kb(FA, staff, group, {})
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        assert urls == [f"https://shop.example/admin/{page}"]
        assert all(
            not b.callback_data or Admin.unpack(b.callback_data).section in {"home", "backups"}
            for row in kb.inline_keyboard
            for b in row
        )
    monkeypatch.setattr(get_settings(), "public_url", "")
    assert not [
        b.url for row in nav.group_kb(FA, staff, "addpanel", {}).inline_keyboard for b in row if b.url
    ]
    assert "PUBLIC_URL" in nav.group_text(FA, "addpanel")


async def test_reply_labels_match_across_locales_and_overrides() -> None:
    t = Texts("fa", {"fa": {"adm_nav_users": "مشتریان فروشگاه"}})
    assert t.matches("مشتریان فروشگاه", "adm_nav_users")
    assert t.matches(Texts("en")("adm_nav_users"), "adm_nav_users")


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Admin navigation")
    for role, user in ((StaffRole.OWNER, OWNER), (StaffRole.SUPPORT, SUPPORT)):
        await create_staff(
            session, shop.id, role=role, username=f"nav_{user}", password=None, telegram_id=user
        )
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"send": send, "fake": fake, "dp": dp, "bot": bot}
    await bot.session.close()


async def test_enter_return_and_exit(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, FA("btn_admin")))
    assert FA("adm_nav_stats") in reply_labels(fake.last(SendMessage))
    await send(callback_update(OWNER, Admin(section="home").pack()))
    assert FA("adm_nav_back") in reply_labels(fake.last(SendMessage))
    await send(message_update(OWNER, FA("adm_nav_back")))
    labels = reply_labels(fake.last(SendMessage))
    assert FA("btn_admin") in labels and FA("adm_nav_stats") not in labels


@pytest.mark.parametrize("group", list(nav.GROUPS))
async def test_each_landing_button_opens_a_real_submenu(session, env, group) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, FA(f"adm_nav_{group}")))
    call = fake.last(SendMessage)
    targets = [target for _, target in fake.buttons(call) if target]
    assert Admin(section="home").pack() in targets
    expected = {item.target().pack() for item in nav.entries(group, identity(StaffRole.OWNER))}
    assert expected <= set(targets)
    assert FA("unknown_command") not in fake.texts()
    # Any old inline category button must open the same submenu too.
    await send(callback_update(OWNER, Admin(section="nav", action=group).pack()))
    assert fake.last(EditMessageText).text == nav.group_text(FA, group)


async def test_navigation_interrupts_input_before_state_handlers(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, dp, bot = env["send"], env["fake"], env["dp"], env["bot"]
    await send(message_update(OWNER, "/start"))
    state = dp.fsm.get_context(bot, chat_id=OWNER, user_id=OWNER)
    await state.set_state(AdminStates.adjust)
    await state.update_data(customer_id=123)
    await send(message_update(OWNER, FA("adm_nav_users")))
    assert await state.get_state() is None and await state.get_data() == {}
    assert FA("number_expected") not in fake.texts()


async def test_unauthorized_reply_and_inline_categories_are_denied(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(SUPPORT, "/start"))
    fake.reset()
    await send(message_update(SUPPORT, FA("adm_nav_updates")))
    assert fake.last(SendMessage).text == FA("adm_forbidden")
    fake.reset()
    await send(callback_update(SUPPORT, Admin(section="nav", action="settings").pack()))
    assert fake.last(AnswerCallbackQuery).show_alert
    assert not [call for call in fake.calls if isinstance(call, EditMessageText)]
    await send(message_update(CUSTOMER, "/start"))
    fake.reset()
    await send(message_update(CUSTOMER, FA("adm_nav_finance")))
    assert not [
        c for c in fake.calls if isinstance(c, SendMessage) and c.text == nav.group_text(FA, "finance")
    ]
