"""General settings hub and the main-options table: only real settings, safe switches."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import select

from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_config, admin_hub
from paurubot.bot.routers import admin_general as gs
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.models import AuditLog, StaffRole
from paurubot.i18n import Texts, catalog, fields
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER, SUPPORT = 9401, 9402


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "")
    shop = await factories.shop(session, "General settings")
    for uid, role in ((OWNER, StaffRole.OWNER), (SUPPORT, StaffRole.SUPPORT)):
        await create_staff(session, shop.id, role=role, username=f"gs_{uid}", password=None, telegram_id=uid)
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    await send(message_update(OWNER, "/start"))
    yield {"shop": shop, "fake": fake, "send": send}
    await bot.session.close()


async def cfg_of(session, env):  # type: ignore[no-untyped-def]
    return await shop_settings.load(session, env["shop"].id, fresh=True)


async def tap(env, action, id=0, page=0, user=OWNER, section="gset"):  # type: ignore[no-untyped-def]
    await env["send"](callback_update(user, Admin(section=section, action=action, id=id, page=page).pack()))


def edited(env) -> list[str]:  # type: ignore[no-untyped-def]
    return [text for text, _ in env["fake"].buttons(env["fake"].last(EditMessageText))]


def test_only_real_groups_and_locales() -> None:
    assert list(admin_hub.FEATURES) == ["main", "users", "sales", "growth"]
    assert list(admin_config.CONFIG)[gs.CRONS_PAGE] == "crons"
    for key in admin_hub.FEATURE_KEYS:
        assert shop_settings.SPECS[key].kind is bool
    fa, en = catalog("fa"), catalog("en")
    keys = {k for k in fa if k.startswith("adm_gs_")}
    assert keys == {k for k in en if k.startswith("adm_gs_")}
    for key in keys:
        assert fields(fa[key]) == fields(en[key]), key
    # Reference items PauruBot does not have must not appear anywhere.
    for fake_feature in ("آنتی اسپم", "بهینه‌ساز هاست", "احراز با لینک", "تیکت مینی"):
        assert not any(fake_feature in value for value in fa.values() if isinstance(value, str))


async def test_hub_and_main_table_show_live_values(session, env) -> None:  # type: ignore[no-untyped-def]
    await env["send"](message_update(OWNER, FA("adm_nav_settings")))
    hub = env["fake"].last(SendMessage)
    texts = [text for text, _ in env["fake"].buttons(hub)]
    assert texts[0] == FA("adm_gs_bot", status=FA("adm_gs_on"))
    assert FA("adm_gs_main") in texts and FA("adm_gs_reterms") in texts
    await tap(env, "cat", section="feat")
    rows = env["fake"].last(EditMessageText).reply_markup.inline_keyboard
    assert [b.text for b in rows[0]] == [FA("adm_feat_col_status"), FA("adm_feat_col_title")]
    assert [b.text for b in rows[1]] == [FA("adm_gs_on"), FA("adm_feat_bot_enabled")]
    assert rows[1][0].callback_data == rows[1][1].callback_data
    assert FA("adm_gs_iran") in [b.text for row in rows for b in row]
    await tap(env, "", section="feat")
    hub_labels = edited(env)
    assert FA("adm_feat_group_payments") in hub_labels
    assert FA("adm_feat_emoji", count=0) in hub_labels


async def test_bot_off_needs_confirmation_and_is_audited(session, env) -> None:  # type: ignore[no-untyped-def]
    await tap(env, "bot", id=0)
    assert (await cfg_of(session, env))["bot_enabled"] is True
    assert env["fake"].last(EditMessageText).text == FA("adm_gs_bot_off_ask")
    index = admin_hub.FEATURE_KEYS.index("bot_enabled")
    await tap(env, "tog", id=index, page=1, section="feat")  # the table asks too
    assert (await cfg_of(session, env))["bot_enabled"] is True
    await tap(env, "botoff", page=gs.MAIN)
    assert (await cfg_of(session, env))["bot_enabled"] is False
    await tap(env, "botoff")  # replay changes nothing
    await tap(env, "bot", id=1)
    await tap(env, "bot", id=1)  # a double tap cannot flip it back off
    assert (await cfg_of(session, env))["bot_enabled"] is True
    rows = (
        await session.scalars(
            select(AuditLog).where(AuditLog.shop_id == env["shop"].id, AuditLog.action == "settings.update")
        )
    ).all()
    assert [r.after for r in rows] == [{"bot_enabled": False}, {"bot_enabled": True}]


async def test_table_switch_carries_wanted_value(session, env) -> None:  # type: ignore[no-untyped-def]
    index = admin_hub.FEATURE_KEYS.index("require_phone")
    for _ in range(2):
        await tap(env, "tog", id=index, page=2, section="feat")
    assert (await cfg_of(session, env))["require_phone"] is True
    await tap(env, "tog", id=index, page=9, section="feat")
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert (await cfg_of(session, env))["require_phone"] is True


async def test_iranian_numbers_and_custom_codes(session, env) -> None:  # type: ignore[no-untyped-def]
    await tap(env, "iran", id=1, page=gs.MAIN)
    await tap(env, "iran", id=1, page=gs.MAIN)
    assert (await cfg_of(session, env))["phone_country_codes"] == ["98"]
    await tap(env, "iran", id=0, page=gs.MAIN)
    assert (await cfg_of(session, env))["phone_country_codes"] == []
    await shop_settings.set_many(session, env["shop"].id, {"phone_country_codes": ["98", "90"]})
    await session.commit()
    await tap(env, "iran", id=0, page=gs.MAIN)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert (await cfg_of(session, env))["phone_country_codes"] == ["98", "90"]


async def test_menu_mode_respects_availability(session, env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    await tap(env, "menu", id=gs.MENU_MODES.index("inline"))
    assert (await cfg_of(session, env))["menu_mode"] == "inline"
    await tap(env, "menu", id=gs.MENU_MODES.index("miniapp"))  # no https address
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert (await cfg_of(session, env))["menu_mode"] == "inline"
    monkeypatch.setattr(get_settings(), "public_url", "https://shop.example")
    await tap(env, "menu", id=gs.MENU_MODES.index("miniapp"))
    assert (await cfg_of(session, env))["menu_mode"] == "miniapp"


async def test_languages_keep_default_and_one(session, env) -> None:  # type: ignore[no-untyped-def]
    default = env["shop"].default_locale
    other = next(code for code in ("fa", "en") if code != default)
    await tap(env, "lang", id=("fa", "en").index(default), page=0)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    await tap(env, "lang", id=("fa", "en").index(other), page=0)
    assert (await cfg_of(session, env))["languages"] == [default]
    await tap(env, "lang", id=("fa", "en").index(other), page=1)
    assert sorted((await cfg_of(session, env))["languages"]) == ["en", "fa"]
    await tap(env, "lang", id=7, page=1)
    assert env["fake"].last(AnswerCallbackQuery).show_alert


async def test_support_username_input(session, env) -> None:  # type: ignore[no-untyped-def]
    await tap(env, "support")
    await env["send"](message_update(OWNER, "not valid!"))
    assert env["fake"].last(SendMessage).text == FA("adm_gs_support_invalid")
    await env["send"](message_update(OWNER, "good_support"))
    assert (await cfg_of(session, env))["support_username"] == "@good_support"
    await tap(env, "support")
    await env["send"](message_update(OWNER, "-"))
    assert (await cfg_of(session, env))["support_username"] == ""


async def test_terms_again_bumps_only_the_shown_version(session, env) -> None:  # type: ignore[no-untyped-def]
    await tap(env, "terms", id=1)
    await tap(env, "reterms")
    assert (await cfg_of(session, env))["terms_version"] == 1
    await tap(env, "retermsok", id=1)
    await tap(env, "retermsok", id=1)  # stale confirmation
    cfg = await cfg_of(session, env)
    assert cfg["terms_required"] is True and cfg["terms_version"] == 2


async def test_support_staff_cannot_change_general_settings(session, env) -> None:  # type: ignore[no-untyped-def]
    await env["send"](message_update(SUPPORT, "/start"))
    env["fake"].reset()
    await tap(env, "bot", id=0, user=SUPPORT)
    await tap(env, "botoff", user=SUPPORT)
    await tap(env, "terms", id=1, user=SUPPORT)
    cfg = await cfg_of(session, env)
    assert cfg["bot_enabled"] is True and cfg["terms_required"] is False
    assert not [c for c in env["fake"].calls if isinstance(c, EditMessageText)]


async def test_other_pages_link_real_settings(session, env) -> None:  # type: ignore[no-untyped-def]
    groups = list(admin_hub.FEATURES)
    await tap(env, "cat", page=groups.index("users"), section="feat")
    msg = env["fake"].last(EditMessageText)
    assert msg.text == FA("adm_feat_text_users")
    rows = msg.reply_markup.inline_keyboard
    first = [b.text for b in rows[0]]
    assert first == [
        FA("adm_feat_link_requests"),
        FA("adm_gs_on"),
        FA("adm_feat_reseller_applications_enabled"),
    ]
    assert FA("adm_tickets") in [b.text for row in rows for b in row]
    await tap(env, "cat", page=groups.index("sales"), section="feat")
    rows = env["fake"].last(EditMessageText).reply_markup.inline_keyboard
    card = next(row for row in rows if row[-1].text == FA("adm_feat_info_card_enabled"))
    assert len(card) == 3 and card[0].text == FA("adm_gs_theme", value=FA("adm_gs_theme_midnight"))
    # The colour button cycles to the theme it shows next and stays on the sales page.
    await env["send"](callback_update(OWNER, card[0].callback_data))
    await env["send"](callback_update(OWNER, card[0].callback_data))  # stale repeat: same value
    assert (await cfg_of(session, env))["info_card_theme"] == "paper"
    assert env["fake"].last(EditMessageText).text == FA("adm_feat_text_sales")
    await tap(env, "theme", id=99, page=groups.index("sales") + 1)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    # Every settings link points at a section some router really handles.
    for _, target, _ in admin_hub.SETTINGS_LINKS.values():
        await env["send"](callback_update(OWNER, target.pack()))
        assert FA("unknown_command") not in env["fake"].texts()


async def test_support_staff_do_not_see_request_links(session, env) -> None:  # type: ignore[no-untyped-def]
    from paurubot.bot.context import StaffIdentity

    staff = StaffIdentity(role=StaffRole.OWNER, staff=None, permissions=frozenset({"settings.write"}))
    kb = admin_hub.group_kb(FA, "users", {}, staff)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert FA("adm_feat_link_requests") not in labels and FA("adm_tickets") not in labels
