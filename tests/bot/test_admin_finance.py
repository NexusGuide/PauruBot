"""Payment-settings UI tests: no money moves; mutations require fresh confirmed intent."""

from __future__ import annotations

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import func, select

from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_finance as ui
from paurubot.config import get_settings
from paurubot.core import settings as shop_settings
from paurubot.db.models import AuditLog, Payment, Staff, StaffRole
from paurubot.i18n import Texts, catalog, fields
from paurubot.payments import engine
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER, SUPPORT, FINANCE = 8131, 8132, 8133
CARD = {"number": "4111111111111111", "holder": "Example", "bank": "Test"}


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://finance.example")
    shop = await factories.shop(session, "Finance UI")
    for uid, role in ((OWNER, StaffRole.OWNER), (SUPPORT, StaffRole.SUPPORT), (FINANCE, StaffRole.FINANCE)):
        await create_staff(session, shop.id, role=role, username=f"fin_{uid}", password=None, telegram_id=uid)
    await shop_settings.set_many(
        session, shop.id, {"card_accounts": [CARD], "nowpayments_api_key": "PRIVATE_API_KEY"}
    )
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "bot": bot, "fake": fake, "dp": dp, "send": send}
    await bot.session.close()


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [s for s, _ in fake.buttons(fake.last(kind))]


def confirm_target(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return next(v for s, v in fake.buttons(fake.last(kind)) if s == FA("adm_f_apply"))


async def setting_preview(env, key, value=True):  # type: ignore[no-untyped-def]
    await env["send"](
        callback_update(
            OWNER, Admin(section="fset", action="on" if value else "off", id=ui.KEYS.index(key)).pack()
        )
    )
    return confirm_target(env["fake"])


async def numbers(env, key, value):  # type: ignore[no-untyped-def]
    await env["send"](
        callback_update(OWNER, Admin(section="fset", action="ask", id=ui.KEYS.index(key)).pack())
    )
    await env["send"](message_update(OWNER, str(value)))
    return confirm_target(env["fake"], SendMessage)


def test_every_finance_key_translated_and_nonsecret_whitelist() -> None:
    fa, en = catalog("fa"), catalog("en")
    for key in [k for k in fa if k.startswith("adm_f_")]:
        assert key in en and fields(fa[key]) == fields(en[key])
    assert len(ui.KEYS) == len(set(ui.KEYS))
    assert all(key in shop_settings.SPECS and not shop_settings.SPECS[key].secret for key in ui.KEYS)
    assert set(ui.GATEWAYS) - {"crypto"} <= engine.PROVIDERS.keys()


async def test_finance_reply_entry_and_three_column_gateway_list(session, env):  # type: ignore[no-untyped-def]
    await env["send"](message_update(OWNER, FA("adm_nav_finance")))
    assert FA("adm_f_gateways") in labels(env["fake"], SendMessage)
    await env["send"](callback_update(OWNER, Admin(section="fgw").pack()))
    result = env["fake"].last(EditMessageText)
    rows = result.reply_markup.inline_keyboard
    assert [b.text for b in rows[0]] == [FA("adm_f_operations"), FA("adm_f_status"), FA("adm_f_title")]
    assert len(rows[1 : 1 + len(ui.GATEWAYS)]) == len(ui.GATEWAYS)
    assert all(len(row) == 3 for row in rows[1 : 1 + len(ui.GATEWAYS)])
    assert rows[1][1].text == FA("adm_f_on")
    assert CARD["number"] not in result.text and "PRIVATE_API_KEY" not in result.text
    assert all(len(b.callback_data.encode()) <= 64 for row in rows for b in row if b.callback_data)


async def test_no_method_is_enabled_just_by_opening_pages(session, env):  # type: ignore[no-untyped-def]
    before = await shop_settings.load(session, env["shop"].id, fresh=True)
    for section in ("fgw", "fcard", "faccounts", "fbonus"):
        await env["send"](callback_update(OWNER, Admin(section=section).pack()))
    after = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert (
        before == after and not after["card_auto_approve"] and after["card_auto_approve_after_minutes"] == 0
    )
    assert await session.scalar(select(func.count(Payment.id))) == 0


async def test_gateway_prerequisites_use_real_provider_requirements(session, env):  # type: ignore[no-untyped-def]
    # A key alone is insufficient: NOWPayments also needs an IPN secret and an exchange rate.
    await env["send"](
        callback_update(
            OWNER, Admin(section="fset", action="on", id=ui.KEYS.index("nowpayments_enabled")).pack()
        )
    )
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["nowpayments_enabled"]
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {"nowpayments_ipn_secret": "PRIVATE_IPN", "usdt_rate": 90_000, "fx_source": "manual"},
    )
    await session.commit()
    target = await setting_preview(env, "nowpayments_enabled")
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["nowpayments_enabled"]
    await env["send"](callback_update(OWNER, target))
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert cfg["nowpayments_enabled"] and await ui.ready("nowpayments", cfg)
    assert "PRIVATE_" not in " ".join(env["fake"].texts())


async def test_confirmation_replay_creates_one_audit_and_no_payments(session, env):  # type: ignore[no-untyped-def]
    target = await setting_preview(env, "card_enabled", False)
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]
    await env["send"](callback_update(OWNER, target))
    await env["send"](callback_update(OWNER, target))
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]
    logs = list(await session.scalars(select(AuditLog).where(AuditLog.action == "settings.finance_update")))
    assert len(logs) == 1 and logs[0].after == {"card_enabled": False}
    assert await session.scalar(select(func.count(Payment.id))) == 0


async def test_stale_setting_and_changed_credentials_reject_confirmation(session, env):  # type: ignore[no-untyped-def]
    target = await numbers(env, "max_topup", 75_000_000)
    await shop_settings.set_many(session, env["shop"].id, {"max_topup": 60_000_000})
    await session.commit()
    await env["send"](callback_update(OWNER, target))
    assert env["fake"].last(AnswerCallbackQuery).text == FA("adm_f_changed")
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["max_topup"] == 60_000_000
    await shop_settings.set_many(session, env["shop"].id, {"zarinpal_merchant": "MERCHANT_TEST"})
    await session.commit()
    target = await setting_preview(env, "zarinpal_enabled")
    await shop_settings.set_many(session, env["shop"].id, {"zarinpal_merchant": ""})
    await session.commit()
    await env["send"](callback_update(OWNER, target))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["zarinpal_enabled"]


async def test_confirmation_expiry_and_other_actor_cannot_apply(session, env):  # type: ignore[no-untyped-def]
    target = await setting_preview(env, "card_enabled", False)
    await env["send"](callback_update(FINANCE, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]
    state = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    data = await state.get_data()
    data["finance_pending"]["at"] -= ui.TTL + 1
    await state.set_data(data)
    await env["send"](callback_update(OWNER, target))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]


async def test_masked_accounts_confirm_state_and_detect_reorder(session, env):  # type: ignore[no-untyped-def]
    await env["send"](callback_update(OWNER, Admin(section="faccounts").pack()))
    text = env["fake"].last(EditMessageText).text
    assert "1111" in text and CARD["number"] not in text
    await env["send"](callback_update(OWNER, Admin(section="fset", action="acc_off", id=0).pack()))
    target = confirm_target(env["fake"])
    await shop_settings.set_many(session, env["shop"].id, {"card_accounts": [{**CARD, "holder": "Changed"}]})
    await session.commit()
    await env["send"](callback_update(OWNER, target))
    assert env["fake"].last(AnswerCallbackQuery).text == FA("adm_f_changed")
    await env["send"](callback_update(OWNER, Admin(section="fset", action="acc_off", id=0).pack()))
    await env["send"](callback_update(OWNER, confirm_target(env["fake"])))
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert cfg["card_accounts"][0]["disabled"] and not await engine.PROVIDERS["card"].enabled(cfg)
    log = await session.scalar(select(AuditLog).where(AuditLog.action == "settings.finance_update"))
    assert CARD["number"] not in repr(log.after)


@pytest.mark.parametrize(
    "key,bad",
    [
        ("max_topup", "0"),
        ("card_auto_min_history", "0"),
        ("card_auto_max_amount", "0"),
        ("stars_rate", "-1"),
        ("card_auto_approve_after_minutes", "10081"),
    ],
)
async def test_numeric_invalid_input_keeps_current_value(session, env, key, bad):  # type: ignore[no-untyped-def]
    before = (await shop_settings.load(session, env["shop"].id, fresh=True))[key]
    await env["send"](
        callback_update(OWNER, Admin(section="fset", action="ask", id=ui.KEYS.index(key)).pack())
    )
    await env["send"](message_update(OWNER, bad))
    assert env["fake"].last(SendMessage).text == FA("adm_f_invalid_or_not_ready")
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))[key] == before


async def test_numeric_confirmation_persian_input_and_bound_relationship(session, env):  # type: ignore[no-untyped-def]
    target = await numbers(env, "min_topup", "۳۰٬۰۰۰")
    await env["send"](callback_update(OWNER, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["min_topup"] == 30_000
    await env["send"](
        callback_update(OWNER, Admin(section="fset", action="ask", id=ui.KEYS.index("min_topup")).pack())
    )
    await env["send"](message_update(OWNER, "999999999"))
    assert env["fake"].last(SendMessage).text == FA("adm_f_invalid_or_not_ready")


async def test_auto_approval_warning_and_defaults_preserved_until_confirm(session, env):  # type: ignore[no-untyped-def]
    target = await setting_preview(env, "card_auto_approve")
    assert FA("adm_f_auto_warning") in env["fake"].last(EditMessageText).text
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["card_auto_approve"]
    await env["send"](callback_update(OWNER, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_auto_approve"]
    target = await numbers(env, "card_auto_approve_after_minutes", "60")
    assert FA("adm_f_auto_warning") in env["fake"].last(SendMessage).text
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))[
        "card_auto_approve_after_minutes"
    ] == 0
    await env["send"](callback_update(OWNER, Admin(section="fcard").pack()))  # Cancel pending timed change.
    await env["send"](callback_update(OWNER, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))[
        "card_auto_approve_after_minutes"
    ] == 0


async def test_empty_cards_cannot_be_activated_and_invalid_callbacks(session, env):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"card_accounts": [], "card_enabled": False})
    await session.commit()
    await env["send"](
        callback_update(OWNER, Admin(section="fset", action="on", id=ui.KEYS.index("card_enabled")).pack())
    )
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    for section, action, index in (
        ("fset", "ask", 999),
        ("fset", "acc_off", 999),
        ("fgw", "view", 999),
        ("fset", "ask", ui.KEYS.index("card_enabled")),
    ):
        await env["send"](callback_update(OWNER, Admin(section=section, action=action, id=index).pack()))
        assert env["fake"].last(AnswerCallbackQuery).show_alert


async def test_finance_role_sees_reports_not_settings_and_revoked_writer_rejected(session, env):  # type: ignore[no-untyped-def]
    await env["send"](callback_update(FINANCE, Admin(section="fhome").pack()))
    shown = labels(env["fake"])
    assert FA("adm_finance") in shown and FA("adm_f_gateways") not in shown
    target = await setting_preview(env, "card_enabled", False)
    staff = await session.scalar(
        select(Staff).where(Staff.shop_id == env["shop"].id, Staff.telegram_id == OWNER)
    )
    staff.role = StaffRole.SUPPORT
    await session.commit()
    await env["send"](callback_update(OWNER, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]


async def test_card_selection_rewards_web_and_close(session, env):  # type: ignore[no-untyped-def]
    await env["send"](
        callback_update(
            OWNER,
            Admin(
                section="fset",
                action="pick",
                id=ui.KEYS.index("card_pick"),
                page=ui.CARD_PICKS.index("rotate"),
            ).pack(),
        )
    )
    await env["send"](callback_update(OWNER, confirm_target(env["fake"])))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_pick"] == "rotate"
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {
            "topup_bonus_rules": [
                {"provider": "card", "percent": 5, "min_amount": 20_000, "max_bonus": 10_000}
            ]
        },
    )
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="fbonus").pack()))
    result = env["fake"].last(EditMessageText)
    assert "5" in result.text and "20,000" in result.text
    assert "https://finance.example/admin/settings" in [
        b.url for row in result.reply_markup.inline_keyboard for b in row if b.url
    ]
    await env["send"](callback_update(OWNER, Admin(section="fhome", action="close").pack()))
    result = env["fake"].last(EditMessageText)
    assert result.text == FA("adm_f_closed") and result.reply_markup is None


async def test_cross_shop_and_second_owner_confirmation_binding(session, env):  # type: ignore[no-untyped-def]
    other_uid = 8134
    await create_staff(
        session,
        env["shop"].id,
        role=StaffRole.OWNER,
        username="finance_other_owner",
        password=None,
        telegram_id=other_uid,
    )
    await session.commit()
    target = await setting_preview(env, "card_enabled", False)
    await env["send"](callback_update(other_uid, target))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    state = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    data = await state.get_data()
    other = await factories.shop(session, "Other finance")
    data["finance_pending"]["shop_id"] = other.id
    await state.set_data(data)
    await env["send"](callback_update(OWNER, target))
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["card_enabled"]


async def test_bound_dependencies_rechecked_after_preview(session, env):  # type: ignore[no-untyped-def]
    target = await numbers(env, "min_topup", 40_000)
    await shop_settings.set_many(session, env["shop"].id, {"max_topup": 30_000})
    await session.commit()
    await env["send"](callback_update(OWNER, target))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["min_topup"] == 20_000


async def test_incomplete_status_and_actual_payment_availability(session, env):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(session, env["shop"].id, {"nowpayments_enabled": True})
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="fgw").pack()))
    rows = env["fake"].last(EditMessageText).reply_markup.inline_keyboard
    assert rows[1 + ui.GATEWAYS.index("nowpayments")][1].text == FA("adm_f_incomplete")
    assert "card" in await engine.available(session, env["shop"].id)
    target = await setting_preview(env, "card_enabled", False)
    await env["send"](callback_update(OWNER, target))
    assert "card" not in await engine.available(session, env["shop"].id)


async def test_numeric_input_permission_revoked_and_missing_web(session, env, monkeypatch):  # type: ignore[no-untyped-def]
    await env["send"](
        callback_update(OWNER, Admin(section="fset", action="ask", id=ui.KEYS.index("max_topup")).pack())
    )
    row = await session.scalar(
        select(Staff).where(Staff.shop_id == env["shop"].id, Staff.telegram_id == OWNER)
    )
    row.role = StaffRole.SUPPORT
    await session.commit()
    await env["send"](message_update(OWNER, "100000"))
    assert env["fake"].last(SendMessage).text == FA("adm_forbidden")
    assert (await shop_settings.load(session, env["shop"].id, fresh=True))["max_topup"] == 50_000_000
    assert ui.web_button(FA) is not None
    monkeypatch.setattr(get_settings(), "public_url", "")
    assert ui.web_button(FA) is None


async def test_disabling_unsafe_existing_policy_is_never_blocked(session, env):  # type: ignore[no-untyped-def]
    await shop_settings.set_many(
        session,
        env["shop"].id,
        {"card_auto_approve": True, "card_auto_max_amount": 0, "card_auto_min_history": 0},
    )
    await session.commit()
    target = await setting_preview(env, "card_auto_approve", False)
    await env["send"](callback_update(OWNER, target))
    assert not (await shop_settings.load(session, env["shop"].id, fresh=True))["card_auto_approve"]
