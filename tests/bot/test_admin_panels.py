"""Panel manager and actual purchase-policy integration, without production panels."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from fastapi import HTTPException
from sqlalchemy import select

from paurubot.auth import rbac
from paurubot.auth.staff import create_staff
from paurubot.bot import keyboards
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_panels
from paurubot.core import orders, trials
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    Service,
    ServiceStatus,
    StaffRole,
    UsernameMethod,
)
from paurubot.i18n import Texts, catalog, fields
from paurubot.panels import GB, PanelError, registry
from paurubot.provisioning.lifecycle import ProvisionError, _panel_for_new, renew_mode
from tests import factories
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER, READER = 8021, 8022


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    shop = await factories.shop(session, "Panel manager")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel)
    fake_panel = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake_panel)
    for uid, role in ((OWNER, StaffRole.OWNER), (READER, StaffRole.SUPPORT)):
        await create_staff(session, shop.id, role=role, username=f"pm_{uid}", password=None, telegram_id=uid)
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "plan": plan, "bot": bot, "fake": fake, "send": send, "dp": dp}
    await bot.session.close()
    await registry.close_all()
    registry.test_transports.clear()


def test_panel_translation_parity_and_clean_api_options() -> None:
    from paurubot.api.routes.admin.panels import _clean_options

    fa, en = catalog("fa"), catalog("en")
    for key in [k for k in fa if k.startswith("adm_pm_")]:
        assert key in en and fields(fa[key]) == fields(en[key])
    assert _clean_options({"allow_trials": False, "allow_renewals": True}) == {
        "allow_trials": False,
        "allow_renewals": True,
    }
    for value in ("false", 0, [], {}):
        with pytest.raises(HTTPException):
            _clean_options({"allow_trials": value})


async def test_panels_list_opens_management_and_paginates(session, env):  # type: ignore[no-untyped-def]
    for _ in range(11):
        await factories.panel(session, env["shop"])
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="panels").pack()))
    fake = env["fake"]
    result = fake.last(EditMessageText)
    targets = [v for _, v in fake.buttons(result) if v and Admin.unpack(v).section == "pmgr"]
    assert len(targets) == 10
    assert Admin(section="panels", page=1).pack() in [v for _, v in fake.buttons(result)]
    await env["send"](callback_update(OWNER, targets[0]))
    result = fake.last(EditMessageText)
    assert FA("adm_pm_features") in [s for s, _ in fake.buttons(result)]
    assert FA("adm_pm_capabilities") in [s for s, _ in fake.buttons(result)]
    assert env["panel"].password_enc not in result.text
    await env["send"](callback_update(OWNER, Admin(section="panels", page=999).pack()))
    result = fake.last(EditMessageText)
    assert len([v for _, v in fake.buttons(result) if v and Admin.unpack(v).section == "pmgr"]) == 2


async def test_feature_switches_set_not_toggle_and_preserve_secrets(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    panel.options = {"api_token_enc": "UNTOUCHED_SECRET", "group_ids": [1, 2]}
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="pfeat", id=panel.id).pack()))
    result = env["fake"].last(EditMessageText)
    assert all(len(row) == 2 for row in result.reply_markup.inline_keyboard[: len(admin_panels.FEATURES)])
    for index, feature in enumerate(admin_panels.FEATURES):
        target = Admin(section="pfeat", action="off", id=panel.id, page=index).pack()
        await env["send"](callback_update(OWNER, target))
        await env["send"](callback_update(OWNER, target))
        await session.refresh(panel)
        assert not panel.is_active if feature.key == "is_active" else panel.options[feature.key] is False
    assert panel.options["api_token_enc"] == "UNTOUCHED_SECRET" and panel.options["group_ids"] == [1, 2]
    logs = list(
        await session.scalars(
            select(AuditLog).where(AuditLog.entity_id == panel.id, AuditLog.action == "panel.bot_update")
        )
    )
    # Repeating the same desired state must not create a second audit entry.
    assert len(logs) == len(admin_panels.FEATURES)
    assert "UNTOUCHED_SECRET" not in repr([(row.before, row.after) for row in logs])
    assert "UNTOUCHED_SECRET" not in " ".join(env["fake"].texts())


async def test_reset_inherit_and_product_precedence(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    panel.options = {"renew_resets_usage": False, "group_ids": [3]}
    await session.commit()
    await env["send"](
        callback_update(OWNER, Admin(section="pfeat", action="inherit", id=panel.id, page=5).pack())
    )
    await session.refresh(panel)
    assert "renew_resets_usage" not in panel.options and panel.options["group_ids"] == [3]
    cfg = await shop_settings.load(session, panel.shop_id)
    assert renew_mode(None, panel, cfg) == renew_mode(None, None, cfg)
    env["plan"].renew_mode = "restart_carry"
    panel.options = {"renew_resets_usage": True}
    assert renew_mode(env["plan"], panel, cfg) == "restart_carry"


async def test_unsupported_driver_cannot_enable_extras(session, env):  # type: ignore[no-untyped-def]
    panel = await factories.panel(session, env["shop"], kind=PanelKind.IBSNG)
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="pfeat", id=panel.id).pack()))
    rows = env["fake"].last(EditMessageText).reply_markup.inline_keyboard
    assert rows[3][0].text == FA("adm_pm_unsupported")
    await env["send"](callback_update(OWNER, Admin(section="pfeat", action="on", id=panel.id, page=3).pack()))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    await session.refresh(panel)
    assert "sell_extra_volume" not in panel.options
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="caps", id=panel.id).pack()))
    assert FA("adm_pm_unsupported") in [s for s, _ in env["fake"].buttons(env["fake"].last(EditMessageText))]


async def test_reader_and_foreign_panel_cannot_mutate(session, env, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setitem(rbac.MATRIX, StaffRole.SUPPORT, frozenset({"panels.read"}))
    panel = env["panel"]
    await env["send"](callback_update(READER, Admin(section="pmgr", id=panel.id).pack()))
    labels = [s for s, _ in env["fake"].buttons(env["fake"].last(EditMessageText))]
    assert FA("adm_pm_name") not in labels and FA("adm_pm_finance") not in labels
    for section, action in (
        ("pfeat", "off"),
        ("pmgr", "name"),
        ("pmgr", "test"),
        ("pmgr", "finance"),
        ("pmgr", "plans"),
    ):
        await env["send"](callback_update(READER, Admin(section=section, action=action, id=panel.id).pack()))
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    await session.refresh(panel)
    assert panel.is_active
    other = await factories.shop(session, "Foreign panels")
    foreign = await factories.panel(session, other)
    await session.commit()
    for section, action in (("pmgr", "show"), ("pfeat", "off"), ("pmgr", "name"), ("pmgr", "test")):
        await env["send"](callback_update(OWNER, Admin(section=section, action=action, id=foreign.id).pack()))
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    await session.refresh(foreign)
    assert foreign.is_active


@pytest.mark.parametrize(
    "field,raw,expected",
    [
        ("name", "<New Panel>", "<New Panel>"),
        ("capacity", "۲۵", 25),
        ("capacity", "-", None),
        ("prefix", "TEST_", "test_"),
    ],
)
async def test_panel_input_validation_and_saved_view(session, env, field, raw, expected):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action=field, id=panel.id).pack()))
    await env["send"](message_update(OWNER, raw))
    await session.refresh(panel)
    assert getattr(panel, "username_prefix" if field == "prefix" else field) == expected
    result = env["fake"].last(SendMessage)
    assert FA("adm_pm_features") in [s for s, _ in env["fake"].buttons(result)]
    if field == "name":
        assert "&lt;New Panel&gt;" in result.text


@pytest.mark.parametrize(("raw", "expected"), [("۱۲", 12), ("0", None), ("-", None)])
async def test_trial_quota_is_set_from_the_bot(session, env, raw, expected):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    panel.options = {**(panel.options or {}), trials.PANEL_QUOTA: 3, "inbounds": ["vless:x"]}
    await session.commit()
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="trial_quota", id=panel.id).pack()))
    await env["send"](message_update(OWNER, "abc"))  # not a number: asked again, nothing saved
    await session.refresh(panel)
    assert panel.options[trials.PANEL_QUOTA] == 3
    await env["send"](message_update(OWNER, raw))
    await session.refresh(panel)
    assert panel.options.get(trials.PANEL_QUOTA) == expected
    assert panel.options["inbounds"] == ["vless:x"]  # the other options are kept
    assert trials.panel_quota(panel) == (expected or 0)


async def test_invalid_input_method_picker_and_cancel(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    old_name = panel.name
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="name", id=panel.id).pack()))
    await env["send"](message_update(OWNER, "bad\nname"))
    assert env["fake"].last(SendMessage).text == FA("adm_pm_invalid_name")
    await session.refresh(panel)
    assert panel.name == old_name
    await env["send"](callback_update(OWNER, Admin(section="pmgr", id=panel.id).pack()))
    state = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    assert await state.get_state() is None
    index = admin_panels.METHODS.index(UsernameMethod.RANDOM)
    await env["send"](
        callback_update(OWNER, Admin(section="pmgr", action="method", id=panel.id, page=index).pack())
    )
    await session.refresh(panel)
    assert panel.username_method == UsernameMethod.RANDOM
    await env["send"](
        callback_update(OWNER, Admin(section="pmgr", action="method", id=panel.id, page=999).pack())
    )
    assert env["fake"].last(AnswerCallbackQuery).show_alert


async def test_health_check_success_and_generic_failure(session, env, monkeypatch):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="test", id=panel.id).pack()))
    await session.refresh(panel)
    assert panel.is_healthy and panel.health_checked_at is not None

    class BadDriver:
        health_check = AsyncMock(
            side_effect=PanelError("TOKEN_SECRET at https://private.example/?token=secret")
        )

    monkeypatch.setattr(registry, "driver_for", AsyncMock(return_value=BadDriver()))
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="test", id=panel.id).pack()))
    await session.refresh(panel)
    assert panel.is_healthy is False and panel.health_error == "bot_health_check_failed"
    assert "TOKEN_SECRET" not in env["fake"].last(EditMessageText).text


async def test_trial_toggle_changes_selection_and_provisioning(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    other = await factories.panel(session, env["shop"])
    panel.options = {"allow_trials": False}
    await session.commit()
    with pytest.raises(trials.TrialError, match="trial_disabled"):
        await trials._trial_panel(session, panel.shop_id, panel.id)
    assert (await trials._trial_panel(session, panel.shop_id, None)).id == other.id
    order = Order(
        shop_id=panel.shop_id,
        customer_id=(await factories.customer(session, env["shop"])).id,
        kind=OrderKind.TRIAL,
        status=OrderStatus.PAID,
        amount=0,
        params={"panel_id": panel.id},
    )
    session.add(order)
    await session.flush()
    with pytest.raises(ProvisionError):
        await _panel_for_new(session, order)


async def test_renewal_switch_blocks_draft_and_hides_button(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    cust = await factories.customer(session, env["shop"])
    service = Service(
        shop_id=panel.shop_id,
        panel_id=panel.id,
        customer_id=cust.id,
        plan_id=env["plan"].id,
        panel_username="renew_switch",
        status=ServiceStatus.ACTIVE,
        data_limit=GB,
        expire_at=utcnow() + timedelta(days=3),
    )
    session.add(service)
    await session.flush()
    await session.refresh(service)
    panel.options = {"allow_renewals": False}
    await session.commit()
    await session.refresh(service)
    with pytest.raises(orders.OrderError):
        await orders.draft(session, cust, OrderKind.RENEW, service=service, plan=env["plan"])
    markup = keyboards.service_kb(FA, service, extras=(None, None), upgrade=False)
    assert FA("btn_renew") not in [b.text for row in markup.inline_keyboard for b in row]
    panel.options = {"allow_renewals": True}
    await session.commit()
    await session.refresh(service)
    assert (
        await orders.draft(session, cust, OrderKind.RENEW, service=service, plan=env["plan"])
    ).kind == OrderKind.RENEW


async def test_report_excludes_other_panels_unfinished_free_and_trials_amount(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    cust = await factories.customer(session, env["shop"])
    service = Service(
        shop_id=panel.shop_id,
        panel_id=panel.id,
        customer_id=cust.id,
        plan_id=env["plan"].id,
        panel_username="report",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.flush()
    for amount, status, kind in [
        (120_000, OrderStatus.COMPLETED, OrderKind.PURCHASE),
        (30_000, OrderStatus.COMPLETED, OrderKind.RENEW),
        (0, OrderStatus.COMPLETED, OrderKind.PURCHASE),
        (990_000, OrderStatus.PENDING, OrderKind.PURCHASE),
        (500_000, OrderStatus.COMPLETED, OrderKind.TRIAL),
    ]:
        session.add(
            Order(
                shop_id=panel.shop_id,
                customer_id=cust.id,
                service_id=service.id,
                kind=kind,
                status=status,
                amount=amount,
                completed_at=utcnow(),
            )
        )
    await session.commit()
    text = await admin_panels.financial_text(session, FA, panel)
    assert "150,000" in text and "5,000" in text
    assert "990,000" not in text and "500,000" not in text
    other = await factories.panel(session, env["shop"])
    assert "150,000" not in await admin_panels.financial_text(session, FA, other)


async def test_disabled_trial_panel_blocks_real_request_and_fallback(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    panel.options = {"allow_trials": False}
    cust = await factories.customer(session, env["shop"])
    await shop_settings.set_many(session, panel.shop_id, {"trial_panel_id": panel.id})
    await session.commit()
    with pytest.raises(trials.TrialError, match="trial_disabled"):
        await trials.request(session, cust)
    second = await factories.panel(session, env["shop"])
    await shop_settings.set_many(session, panel.shop_id, {"trial_panel_id": 0})
    await session.commit()
    order = await trials.request(session, cust)
    assert order.params["panel_id"] == second.id


async def test_unknown_feature_callbacks_and_permissions_lost_during_input(session, env, monkeypatch):  # type: ignore[no-untyped-def]
    from paurubot.db.models import Staff

    panel = env["panel"]
    for action, index in (("off", 999), ("inherit", 0), ("invalid", 0)):
        await env["send"](
            callback_update(OWNER, Admin(section="pfeat", action=action, id=panel.id, page=index).pack())
        )
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="name", id=panel.id).pack()))
    owner = await session.scalar(
        select(Staff).where(Staff.shop_id == panel.shop_id, Staff.telegram_id == OWNER)
    )
    owner.role = StaffRole.SUPPORT
    monkeypatch.setitem(rbac.MATRIX, StaffRole.SUPPORT, frozenset({"panels.read"}))
    await session.commit()
    previous = panel.name
    await env["send"](message_update(OWNER, "Unauthorized rename"))
    await session.refresh(panel)
    assert panel.name == previous
    assert env["fake"].last(SendMessage).text == FA("adm_forbidden")


async def test_extra_switches_have_real_price_effect_and_close(session, env):  # type: ignore[no-untyped-def]
    panel = env["panel"]
    cust = await factories.customer(session, env["shop"])
    service = Service(
        shop_id=panel.shop_id,
        panel_id=panel.id,
        customer_id=cust.id,
        plan_id=env["plan"].id,
        panel_username="extra_flags",
        status=ServiceStatus.ACTIVE,
    )
    session.add(service)
    await session.flush()
    await session.refresh(service)
    await shop_settings.set_many(
        session, panel.shop_id, {"extras_enabled": True, "extra_gb_price": 20_000, "extra_day_price": 5_000}
    )
    await session.commit()
    cfg = await shop_settings.load(session, panel.shop_id, fresh=True)
    assert orders.service_extras(cfg, service) == (20_000, 5_000)
    for index in (3, 4):
        await env["send"](
            callback_update(OWNER, Admin(section="pfeat", action="off", id=panel.id, page=index).pack())
        )
    await session.refresh(panel)
    await session.refresh(service)
    assert orders.service_extras(cfg, service) == (None, None)
    await env["send"](callback_update(OWNER, Admin(section="pmgr", action="close", id=panel.id).pack()))
    result = env["fake"].last(EditMessageText)
    assert result.text == FA("adm_pm_closed") and result.reply_markup is None
