"""Shop editor dispatcher flows, policy enforcement, tenant isolation and confirmed writes."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from sqlalchemy import func, select

from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_shop as ui
from paurubot.config import get_settings
from paurubot.core import bulk, orders
from paurubot.core import catalog as core_catalog
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    AuditLog,
    Category,
    Location,
    Order,
    OrderKind,
    OrderStatus,
    PanelKind,
    Plan,
    PlanAudience,
    Service,
    Staff,
    StaffRole,
)
from paurubot.i18n import Texts, catalog, fields
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER, SUPPORT, OTHER = 8311, 8312, 8313


@pytest.fixture
async def env(session, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "public_url", "https://shop.example")
    shop = await factories.shop(session, "Shop editor")
    panel = await factories.panel(session, shop)
    plan = await factories.plan(session, shop, panel, title="Monthly")
    customer = await factories.customer(session, shop)
    for uid, role in ((OWNER, StaffRole.OWNER), (SUPPORT, StaffRole.SUPPORT), (OTHER, StaffRole.OWNER)):
        await create_staff(
            session, shop.id, role=role, username=f"shop_{uid}", password=None, telegram_id=uid
        )
    await session.commit()
    bot, fake = make_bot()
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {
        "shop": shop,
        "panel": panel,
        "plan": plan,
        "customer": customer,
        "bot": bot,
        "dp": dp,
        "fake": fake,
        "send": send,
    }
    await bot.session.close()


async def click(env, section="pedit", action="show", id=None, page=0, user=OWNER):  # type: ignore[no-untyped-def]
    await env["send"](
        callback_update(
            user,
            Admin(section=section, action=action, id=env["plan"].id if id is None else id, page=page).pack(),
        )
    )


def target(env, method=EditMessageText):  # type: ignore[no-untyped-def]
    return next(v for label, v in env["fake"].buttons(env["fake"].last(method)) if label == FA("adm_s_apply"))


async def edit(env, key, value):  # type: ignore[no-untyped-def]
    await click(env, action="ask", page=ui.FIELDS.index(key))
    await env["send"](message_update(OWNER, str(value)))
    return target(env, SendMessage)


async def apply(env, callback):  # type: ignore[no-untyped-def]
    await env["send"](callback_update(OWNER, callback))


async def test_shop_landing_and_editor_layout(session, env):  # type: ignore[no-untyped-def]
    await env["send"](message_update(OWNER, FA("adm_nav_shop")))
    labels = [s for s, _ in env["fake"].buttons(env["fake"].last(SendMessage))]
    for key in (
        "adm_plans",
        "adm_s_categories",
        "adm_s_gift_add",
        "adm_s_gift_manage",
        "adm_s_discount_add",
        "adm_s_discount_manage",
        "adm_s_bulk",
    ):
        assert FA(key) in labels
    await click(env, section="plans", id=0)
    result = env["fake"].last(EditMessageText)
    assert any(
        Admin.unpack(v).section == "pedit" for _, v in env["fake"].buttons(result) if v.startswith("a:")
    )
    await click(env)
    result = env["fake"].last(EditMessageText)
    assert [b.text for b in result.reply_markup.inline_keyboard[0]] == [
        FA("adm_s_field_price"),
        FA("adm_s_field_data_gb"),
        FA("adm_s_field_days"),
    ]
    assert "Monthly" in result.text and "{title}" not in result.text
    for row in result.reply_markup.inline_keyboard:
        for b in row:
            assert not b.callback_data or len(b.callback_data.encode()) <= 64
    await click(env, section="plan", action="", id=env["plan"].id)
    assert FA("adm_s_renew_mode") in [s for s, _ in env["fake"].buttons(env["fake"].last(EditMessageText))]


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("price", "۵۰٬۰۰۰", 50000),
        ("days", "60", 60),
        ("data_gb", "100", 100),
        ("ip_limit", "3", 3),
        ("sort", "-2", -2),
        ("title", "Premium", "Premium"),
        ("description", "<b>note</b>", "<b>note</b>"),
        ("username_prefix", "VIP_", "vip_"),
    ],
)
async def test_reviewed_field_changes(session, env, key, value, expected):  # type: ignore[no-untyped-def]
    old = getattr(env["plan"], key)
    callback = await edit(env, key, value)
    await session.refresh(env["plan"])
    assert getattr(env["plan"], key) == old
    await apply(env, callback)
    await session.refresh(env["plan"])
    assert getattr(env["plan"], key) == expected
    await apply(env, callback)
    logs = list(await session.scalars(select(AuditLog).where(AuditLog.action == "plan.bot_update")))
    assert len(logs) == 1 and logs[0].after == {key: expected}
    assert await session.scalar(select(func.count(Service.id))) == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("price", "-1"),
        ("days", "3651"),
        ("data_gb", "100001"),
        ("ip_limit", "101"),
        ("title", "x" * 97),
        ("description", "x" * 1001),
        ("username_prefix", "bad-prefix"),
        ("sort", "1000001"),
    ],
)
async def test_invalid_input_no_write(session, env, key, value):  # type: ignore[no-untyped-def]
    old = getattr(env["plan"], key)
    await click(env, action="ask", page=ui.FIELDS.index(key))
    await env["send"](message_update(OWNER, value))
    assert env["fake"].last(SendMessage).text == FA("adm_s_invalid_input")
    await session.refresh(env["plan"])
    assert getattr(env["plan"], key) == old


async def test_audience_and_renewal_use_real_values(session, env):  # type: ignore[no-untyped-def]
    await click(env, action="set_aud", page=2)
    await apply(env, target(env))
    await session.refresh(env["plan"])
    assert env["plan"].audience == PlanAudience.RESELLERS
    assert not await core_catalog.plans(session, env["shop"].id, env["customer"])
    await click(env, action="set_renew", page=4)
    await apply(env, target(env))
    await session.refresh(env["plan"])
    assert env["plan"].renew_mode == "restart_carry"
    await click(env, action="set_renew", page=0)
    await apply(env, target(env))
    await session.refresh(env["plan"])
    assert env["plan"].renew_mode is None


async def test_category_create_assign_hide_restore(session, env):  # type: ignore[no-untyped-def]
    await click(env, section="scats", action="add", id=0)
    await env["send"](message_update(OWNER, "Premium"))
    callback = target(env, SendMessage)
    assert not await session.scalar(select(Category.id).where(Category.shop_id == env["shop"].id))
    await apply(env, callback)
    category = await session.scalar(select(Category).where(Category.shop_id == env["shop"].id))
    await apply(env, callback)
    assert await session.scalar(select(func.count(Category.id))) == 1
    await click(env, action="set_cat", page=category.id)
    await apply(env, target(env))
    assert len(await core_catalog.plans(session, env["shop"].id, env["customer"])) == 1
    await click(env, section="scats", action="off", id=category.id)
    await apply(env, target(env))
    assert not await core_catalog.plans(session, env["shop"].id, env["customer"])
    assert await core_catalog.get_plan(session, env["shop"].id, env["plan"].id, env["customer"]) is None
    await click(env, section="scats", action="on", id=category.id)
    await apply(env, target(env))
    assert len(await core_catalog.plans(session, env["shop"].id, env["customer"])) == 1


async def test_category_rename_sort_and_duplicate_rejection(session, env):  # type: ignore[no-untyped-def]
    row = Category(shop_id=env["shop"].id, title="Existing")
    session.add(row)
    await session.commit()
    for action, value in (("rename", "Updated"), ("sort", "-5")):
        await click(env, section="scats", action=action, id=row.id)
        await env["send"](message_update(OWNER, value))
        await apply(env, target(env, SendMessage))
    await session.refresh(row)
    assert row.title == "Updated" and row.sort == -5
    await click(env, section="scats", action="add", id=0)
    await env["send"](message_update(OWNER, "Updated"))
    await apply(env, target(env, SendMessage))
    assert env["fake"].last(AnswerCallbackQuery).text == FA("adm_s_category_exists")
    assert await session.scalar(select(func.count(Category.id))) == 1


async def test_stale_expired_cancelled_foreign_actor_and_role_revocation(session, env):  # type: ignore[no-untyped-def]
    callback = await edit(env, "price", 200000)
    await env["send"](callback_update(OTHER, callback))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    env["plan"].title = "Changed elsewhere"
    await session.commit()
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).text == FA("adm_s_changed")
    callback = await edit(env, "price", 200000)
    ctx = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    data = await ctx.get_data()
    data["shop_pending"]["at"] = 0
    await ctx.set_data(data)
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).text == FA("button_expired")
    callback = await edit(env, "price", 200000)
    await click(env)
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    callback = await edit(env, "price", 200000)
    owner = await session.scalar(select(Staff).where(Staff.telegram_id == OWNER))
    owner.role = StaffRole.SUPPORT
    await session.commit()
    await apply(env, callback)
    await session.refresh(env["plan"])
    assert env["plan"].price == 100000


async def test_other_shop_entities_and_foreign_names_are_not_exposed(session, env):  # type: ignore[no-untyped-def]
    foreign = await factories.shop(session, "Foreign")
    panel = await factories.panel(session, foreign)
    panel.name = "PRIVATE_PANEL"
    plan = await factories.plan(session, foreign, panel, title="PRIVATE_PRODUCT")
    category = Category(shop_id=foreign.id, title="PRIVATE_CATEGORY")
    session.add(category)
    await session.commit()
    for section, id in (("pedit", plan.id), ("scats", category.id)):
        await click(env, section=section, action="view" if section == "scats" else "show", id=id)
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    for action, id in (("set_panel", panel.id), ("set_cat", category.id)):
        await click(env, action=action, page=id)
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    assert "PRIVATE_" not in " ".join(env["fake"].texts())


async def test_destination_compatibility_and_stock_forgeries(session, env):  # type: ignore[no-untyped-def]
    other = await factories.panel(session, env["shop"])
    env["plan"].panel_options = {"inbounds": {"vless": ["old"]}}
    await session.commit()
    await click(env, action="set_panel", page=other.id)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    env["plan"].panel_options = {}
    await session.commit()
    await click(env, action="set_panel", page=other.id)
    await apply(env, target(env))
    await session.refresh(env["plan"])
    assert env["plan"].panel_id == other.id
    env["plan"].delivery = "stock"
    await session.commit()
    for action, page in (
        ("ask", ui.FIELDS.index("days")),
        ("set_renew", 1),
        ("on", ui.FIELDS.index("on_hold")),
    ):
        await click(env, action=action, page=page)
        assert env["fake"].last(AnswerCallbackQuery).show_alert
    callback = await edit(env, "price", 90000)
    await apply(env, callback)
    await session.refresh(env["plan"])
    assert env["plan"].price == 90000


async def test_hold_supported_and_unsupported_pools(session, env):  # type: ignore[no-untyped-def]
    await click(env, action="on", page=ui.FIELDS.index("on_hold"))
    await apply(env, target(env))
    await session.refresh(env["plan"])
    assert env["plan"].on_hold
    panel = await factories.panel(session, env["shop"], PanelKind.REMNAWAVE)
    await click(env, action="set_panel", page=panel.id)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    await click(env, action="off", page=ui.FIELDS.index("on_hold"))
    await apply(env, target(env))
    await click(env, action="set_panel", page=panel.id)
    await apply(env, target(env))
    await click(env, action="on", page=ui.FIELDS.index("on_hold"))
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    location = Location(shop_id=env["shop"].id, title="Mixed")
    session.add(location)
    await session.flush()
    panel.location_id = location.id
    await session.commit()
    await session.refresh(env["plan"])
    assert not await ui.holds_supported(session, env["plan"], {"panel_id": None, "location_id": location.id})


@pytest.mark.parametrize("status", [OrderStatus.PENDING, OrderStatus.PAID, OrderStatus.PROVISIONING])
async def test_open_orders_block_terms_but_price_only_affects_new_quotes(session, env, status):  # type: ignore[no-untyped-def]
    draft = await orders.draft(session, env["customer"], OrderKind.PURCHASE, plan=env["plan"])
    draft = await orders.create(session, env["customer"], draft)
    draft.status = status
    await session.commit()
    await click(env, action="ask", page=ui.FIELDS.index("days"))
    await env["send"](message_update(OWNER, "60"))
    assert env["fake"].last(SendMessage).text == FA("adm_s_invalid_refs_or_busy")
    callback = await edit(env, "price", 150000)
    await apply(env, callback)
    await session.refresh(env["plan"])
    await session.refresh(draft)
    assert draft.amount == 100000 and env["plan"].price == 150000
    fresh = await orders.draft(session, env["customer"], OrderKind.PURCHASE, plan=env["plan"])
    fresh = await orders.create(session, env["customer"], fresh)
    assert fresh.amount == 150000


async def test_expired_pending_and_completed_orders_do_not_block_terms(session, env):  # type: ignore[no-untyped-def]
    for status in (OrderStatus.PENDING, OrderStatus.COMPLETED):
        draft = await orders.draft(session, env["customer"], OrderKind.PURCHASE, plan=env["plan"])
        draft = await orders.create(session, env["customer"], draft)
        draft.status = status
        draft.expires_at = utcnow() - timedelta(minutes=1)
    await session.commit()
    callback = await edit(env, "days", 60)
    await apply(env, callback)
    await session.refresh(env["plan"])
    assert env["plan"].days == 60


async def test_category_and_destination_picker_pagination(session, env):  # type: ignore[no-untyped-def]
    for n in range(12):
        session.add(Category(shop_id=env["shop"].id, title=f"Category{n}", sort=n))
    await session.commit()
    await click(env, action="category", page=9999)
    text = env["fake"].last(EditMessageText).text
    assert "2" in text and "9999" not in text
    await click(env, section="scats", id=0, page=9999)
    assert "9999" not in env["fake"].last(EditMessageText).text


async def test_bulk_confirmed_policy_and_stale_maximum(session, env):  # type: ignore[no-untyped-def]
    await click(env, section="sconf", id=0, action="off")
    callback = target(env)
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert cfg["bulk_buy_enabled"]
    await apply(env, callback)
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert bulk.allowed(cfg, env["customer"]) == 0
    for key in ("bulk_buy_enabled", "bulk_buy_resellers_only"):
        await click(
            env,
            section="sconf",
            id=ui.BULK_KEYS.index(key),
            action="on" if key == "bulk_buy_enabled" else "off",
        )
        await apply(env, target(env))
    await click(env, section="sconf", id=1, action="ask")
    await env["send"](message_update(OWNER, "۳۰"))
    callback = target(env, SendMessage)
    await apply(env, callback)
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert bulk.allowed(cfg, env["customer"]) == 30
    await click(env, section="sconf", id=1, action="ask")
    await env["send"](message_update(OWNER, "40"))
    callback = target(env, SendMessage)
    await shop_settings.set_many(session, env["shop"].id, {"bulk_buy_max": 25})
    await session.commit()
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).text == FA("adm_s_changed")
    with pytest.raises(orders.OrderError):
        await bulk.buy_many(session, env["customer"], env["plan"], 1)


async def test_support_no_catalog_mutation(session, env):  # type: ignore[no-untyped-def]
    for section in ("pedit", "scats", "sconf"):
        await click(env, section=section, user=SUPPORT)
    assert (
        await session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action.in_(["plan.bot_update", "category.bot_update", "settings.bulk_update"])
            )
        )
        == 0
    )


def test_locales_fields_and_nonsecret_settings():
    fa, en = catalog("fa"), catalog("en")
    for key in [k for k in fa if k.startswith("adm_s_")]:
        assert key in en and fields(fa[key]) == fields(en[key])
    for key in ui.SNAPSHOT:
        if key in ui.FIELDS or key in {"audience", "renew_mode", "category_id", "panel_id", "location_id"}:
            assert f"adm_s_field_{key}" in fa
    for key in ui.BULK_KEYS:
        assert key in shop_settings.SPECS and not shop_settings.SPECS[key].secret


async def test_product_pagination_clamps_and_keeps_all_records_reachable(session, env):  # type: ignore[no-untyped-def]
    for n in range(12):
        await factories.plan(session, env["shop"], env["panel"], title=f"Product{n}")
    await session.commit()
    await click(env, section="plans", id=0, page=9999)
    result = env["fake"].last(EditMessageText)
    callbacks = [Admin.unpack(v) for _, v in env["fake"].buttons(result) if v.startswith("a:")]
    assert len([v for v in callbacks if v.section == "pedit"]) == 3
    assert "9999" not in result.text
    await click(env, section="plans", id=0, page=-99)
    callbacks = [
        Admin.unpack(v)
        for _, v in env["fake"].buttons(env["fake"].last(EditMessageText))
        if v.startswith("a:")
    ]
    assert len([v for v in callbacks if v.section == "pedit"]) == 10


async def test_stale_during_text_entry_clearable_fields_and_wrong_shop_nonce(session, env):  # type: ignore[no-untyped-def]
    await click(env, action="ask", page=ui.FIELDS.index("price"))
    env["plan"].price = 110000
    await session.commit()
    await env["send"](message_update(OWNER, "120000"))
    assert env["fake"].last(SendMessage).text == FA("adm_s_changed")
    for key in ("description", "username_prefix"):
        callback = await edit(env, key, "temporary")
        await apply(env, callback)
        callback = await edit(env, key, "-")
        await apply(env, callback)
        await session.refresh(env["plan"])
        assert getattr(env["plan"], key) is None
    callback = await edit(env, "price", 120000)
    ctx = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    data = await ctx.get_data()
    data["shop_pending"]["shop"] = env["shop"].id + 1
    await ctx.set_data(data)
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    await session.refresh(env["plan"])
    assert env["plan"].price == 110000


async def test_sale_count_ignores_trials_and_free_orders(session, env):  # type: ignore[no-untyped-def]
    for kind, amount, status in (
        (OrderKind.PURCHASE, 100000, OrderStatus.COMPLETED),
        (OrderKind.PURCHASE, 0, OrderStatus.COMPLETED),
        (OrderKind.TRIAL, 0, OrderStatus.COMPLETED),
        (OrderKind.RENEW, 100000, OrderStatus.COMPLETED),
        (OrderKind.PURCHASE, 100000, OrderStatus.PENDING),
    ):
        session.add(
            Order(
                shop_id=env["shop"].id,
                customer_id=env["customer"].id,
                plan_id=env["plan"].id,
                kind=kind,
                status=status,
                amount=amount,
            )
        )
    await session.commit()
    await click(env)
    assert "خرید موفق پولی: 1" in env["fake"].last(EditMessageText).text


async def test_bulk_invalid_expired_replayed_and_revoked_confirmations(session, env):  # type: ignore[no-untyped-def]
    await click(env, section="sconf", id=1, action="ask")
    for value in ("1", "201", "bad"):
        await env["send"](message_update(OWNER, value))
        assert env["fake"].last(SendMessage).text == FA("adm_s_invalid_input")
    await env["send"](message_update(OWNER, "15"))
    callback = target(env, SendMessage)
    ctx = env["dp"].fsm.get_context(bot=env["bot"], chat_id=OWNER, user_id=OWNER)
    data = await ctx.get_data()
    data["bulk_pending"]["at"] = 0
    await ctx.set_data(data)
    await apply(env, callback)
    assert env["fake"].last(AnswerCallbackQuery).show_alert
    await click(env, section="sconf", id=0, action="off")
    callback = target(env)
    await apply(env, callback)
    await apply(env, callback)
    assert (
        await session.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "settings.bulk_update"))
        == 1
    )
    await click(env, section="sconf", id=0, action="on")
    callback = target(env)
    owner = await session.scalar(select(Staff).where(Staff.telegram_id == OWNER))
    owner.role = StaffRole.SUPPORT
    await session.commit()
    await apply(env, callback)
    cfg = await shop_settings.load(session, env["shop"].id, fresh=True)
    assert not cfg["bulk_buy_enabled"]


async def test_a_category_can_be_deleted_and_its_plans_stay_on_sale(session, env):  # type: ignore[no-untyped-def]
    category = Category(shop_id=env["shop"].id, title="Tunnel")
    session.add(category)
    await session.flush()
    env["plan"].category_id = category.id
    await session.commit()
    category_id, plan_id = category.id, env["plan"].id

    async def exists():  # type: ignore[no-untyped-def]
        return await session.scalar(select(Category.id).where(Category.id == category_id))

    await click(env, section="scats", action="view", id=category_id)
    card = env["fake"].last(EditMessageText)
    assert Texts("fa")("adm_s_category_delete") in [text for text, _ in env["fake"].buttons(card)]
    await click(env, section="scats", action="del", id=category_id)  # asks first: nothing deleted yet
    assert await exists() is not None
    await click(env, section="scats", action="delgo", id=category_id, user=SUPPORT)  # no catalog.write
    assert await exists() is not None
    await click(env, section="scats", action="delgo", id=category_id)
    assert await exists() is None
    assert await session.scalar(select(Plan.category_id).where(Plan.id == plan_id)) is None
    assert await session.scalar(select(Plan.is_active).where(Plan.id == plan_id)) is True
    assert len(await core_catalog.plans(session, env["shop"].id, env["customer"])) == 1
