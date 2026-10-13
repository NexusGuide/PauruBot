"""More staff tools in the bot, through the real dispatcher (ADM-09)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from sqlalchemy import select, update

import paurubot.notifications.handlers  # noqa: F401  (registers the queues)
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_tools
from paurubot.core import settings as shop_settings
from paurubot.db.base import utcnow
from paurubot.db.models import (
    Broadcast,
    Job,
    JobStatus,
    Order,
    Plan,
    ResellerGroup,
    ResellerProfile,
    Service,
    ServiceStatus,
    StaffRole,
)
from paurubot.i18n import Texts
from paurubot.ledger.accounts import wallet_balance
from paurubot.notifications import telegram as notify_telegram
from paurubot.panels import GB, registry
from tests import factories
from tests.bot.test_flows import _customer
from tests.fakes.panels import FakeTokenPanel, transport
from tests.fakes.telegram import callback_update, make_bot, message_update
from tests.unit.test_resellers import drain

FA = Texts("fa")
OWNER = 7501
SUPPORT = 7502


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Admin tools")
    panel = await factories.panel(session, shop)
    fake_panel = FakeTokenPanel("marzban")
    registry.test_transports[panel.id] = transport(fake_panel)
    plan = await factories.plan(session, shop, panel, price=100_000, title="Monthly")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="tools_owner", password=None, telegram_id=OWNER
    )
    await create_staff(
        session, shop.id, role=StaffRole.SUPPORT, username="tools_support", password=None, telegram_id=SUPPORT
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "plan": plan, "fake": fake, "send": send, "fake_panel": fake_panel}
    notify_telegram.set_bot(None)
    registry.test_transports.clear()
    registry._cache.clear()


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [text for text, _ in fake.buttons(fake.last(kind))]


def test_every_dynamic_label_exists() -> None:
    for locale in ("fa", "en"):
        t = Texts(locale)
        keys = [f"adm_set_{k}" for k in admin_tools.SETTING_KEYS]
        keys += [f"adm_set_group_{g}" for g in admin_tools.SETTINGS]
        keys += [f"adm_aud_{a}" for a in admin_tools.AUDIENCES]
        keys += [f"adm_svc_{c}" for c in admin_tools.EXTEND]
        keys += [f"adm_finance_{k}" for k in ("today", "week", "month")]
        for key in keys:
            assert t(key) != key, (locale, key)
    for key in admin_tools.SETTING_KEYS:
        assert not shop_settings.SPECS[key].secret


async def test_home_shows_tools_by_permission(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, FA("btn_admin")))
    owner = [b.text for row in fake.last(SendMessage).reply_markup.keyboard for b in row]
    for key in ("adm_nav_shop", "adm_nav_panels", "adm_nav_finance", "adm_nav_settings"):
        assert FA(key) in owner, key
    await send(message_update(SUPPORT, "/start"))
    await send(message_update(SUPPORT, FA("btn_admin")))
    support = [b.text for row in fake.last(SendMessage).reply_markup.keyboard for b in row]
    assert FA("adm_nav_shop") not in support and FA("adm_nav_settings") not in support
    # A forged button from the support role is refused like any other staff-only action.
    fake.reset()
    await send(callback_update(SUPPORT, Admin(section="settings").pack()))
    assert not [c for c in fake.calls if isinstance(c, EditMessageText)]


async def test_customer_services_extend_disable_and_free_service(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel, plan = env["send"], env["fake"], env["shop"], env["panel"], env["plan"]
    await send(message_update(OWNER, "/start"))
    customer = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="u_card",
        label="Ali",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * GB,
        expire_at=utcnow() + timedelta(days=5),
    )
    session.add(service)
    await session.commit()
    env["fake_panel"].users["u_card"] = {
        "username": "u_card",
        "status": "active",
        "used_traffic": 0,
        "data_limit": 10 * GB,
        "expire": int(service.expire_at.timestamp()),
        "sub": "s1",
    }
    # Lookup by the service's panel username opens the service itself.
    await send(callback_update(OWNER, Admin(section="lookup").pack()))
    await send(message_update(OWNER, "u_card"))
    assert FA("adm_svc_d7") in labels(fake, SendMessage)
    await send(callback_update(OWNER, Admin(section="svcact", action="d30", id=service.id).pack()))
    await session.refresh(service)
    assert service.expire_at is not None and service.expire_at > utcnow() + timedelta(days=34)
    await send(callback_update(OWNER, Admin(section="svcact", action="off", id=service.id).pack()))
    await session.refresh(service)
    assert service.status == ServiceStatus.DISABLED
    alert = await session.scalar(select(Job).where(Job.kind == "service_alert"))
    assert alert is not None and alert.payload["alert"] == "disabled_by_staff"
    # The customer card lists services and gives a free one through the provisioning queue.
    await send(callback_update(OWNER, Admin(section="usvc", id=customer.id).pack()))
    assert any("Ali" in text for text in labels(fake))
    await send(
        callback_update(OWNER, Admin(section="ugive", action="plan", id=customer.id, page=plan.id).pack())
    )
    order = await session.scalar(
        select(Order).where(Order.customer_id == customer.id, Order.source == "staff_bot")
    )
    assert order is not None and order.amount == 0
    await drain(session, "provision")
    await session.refresh(order)
    assert order.status == "completed"


async def test_plan_price_bulk_change_and_hide(session, env) -> None:  # type: ignore[no-untyped-def]
    send, plan = env["send"], env["plan"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="plan", action="price", id=plan.id).pack()))
    await send(message_update(OWNER, "120000"))
    await session.refresh(plan)
    assert plan.price == 120_000
    await send(callback_update(OWNER, Admin(section="pbulk").pack()))
    await send(message_update(OWNER, "-10"))
    await session.refresh(plan)
    assert plan.price == 108_000
    await send(callback_update(OWNER, Admin(section="pbulk").pack()))
    await send(message_update(OWNER, "500"))  # refused: out of range
    await session.refresh(plan)
    assert plan.price == 108_000
    await send(callback_update(OWNER, Admin(section="plan", action="toggle", id=plan.id).pack()))
    assert not (await session.get(Plan, plan.id, populate_existing=True)).is_active


async def test_settings_toggle_and_number(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(OWNER, "/start"))
    key_index = admin_tools.SETTING_KEYS.index("trial_enabled")
    before = await shop_settings.get(session, shop.id, "trial_enabled")
    await send(callback_update(OWNER, Admin(section="settings", action="toggle", id=key_index).pack()))
    assert await shop_settings.load(session, shop.id, fresh=True) and (
        (await shop_settings.load(session, shop.id, fresh=True))["trial_enabled"] is (not before)
    )
    index = admin_tools.SETTING_KEYS.index("referral_percent")
    await send(callback_update(OWNER, Admin(section="settings", action="ask", id=index).pack()))
    await send(message_update(OWNER, "150"))
    assert FA("adm_setting_range", low=0, high=100) in fake.texts()
    await send(message_update(OWNER, "15"))
    assert (await shop_settings.load(session, shop.id, fresh=True))["referral_percent"] == 15


async def test_finance_and_excel(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="finance").pack()))
    assert FA("adm_export_days", days=30) in labels(fake)
    await send(callback_update(OWNER, Admin(section="finance", action="xlsx", page=30).pack()))
    doc = fake.last(SendDocument)
    assert doc.document.filename == "orders-30d.xlsx"


async def test_gift_to_resellers_pays_once(session, env) -> None:  # type: ignore[no-untyped-def]
    send, shop = env["send"], env["shop"]
    await send(message_update(OWNER, "/start"))
    group = ResellerGroup(shop_id=shop.id, title="G", discount_percent=5)
    session.add(group)
    await session.flush()
    reseller = await factories.customer(session, shop)
    other = await factories.customer(session, shop)
    session.add(ResellerProfile(shop_id=shop.id, customer_id=reseller.id, group_id=group.id))
    await session.commit()
    await send(callback_update(OWNER, Admin(section="gift_all").pack()))
    await send(message_update(OWNER, "50000"))
    index = admin_tools.AUDIENCES.index("resellers")
    await send(callback_update(OWNER, Admin(section="gift_all", action="aud", page=index).pack()))
    confirmation = env["fake"].buttons(env["fake"].last(EditMessageText))[0][1]
    assert confirmation is not None
    await send(callback_update(OWNER, confirmation))
    await drain(session, "broadcast")
    assert await wallet_balance(session, shop.id, reseller.id) == 50_000
    assert await wallet_balance(session, shop.id, other.id) == 0
    told = FA("gift_received", amount="50,000")
    assert env["fake"].texts(reseller.telegram_id).count(told) == 1
    # Running the same gift job again pays nobody twice.
    job = await session.scalar(select(Job).where(Job.kind == "gift"))
    assert job is not None
    job.status, job.run_at = JobStatus.QUEUED, utcnow()
    job.payload = {**job.payload, "cursor": 0}
    await session.commit()
    await drain(session, "broadcast")
    assert await wallet_balance(session, shop.id, reseller.id) == 50_000
    assert env["fake"].texts(reseller.telegram_id).count(told) == 1


async def test_broadcast_options_audience_button_and_pin(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel, plan = env["send"], env["fake"], env["shop"], env["panel"], env["plan"]
    await send(message_update(OWNER, "/start"))
    buyer_uid = 7601
    await send(message_update(buyer_uid, "/start"))
    buyer = await _customer(session, shop.id, buyer_uid)
    await send(message_update(buyer_uid + 1, "/start"))
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=buyer.id,
            panel_id=panel.id,
            plan_id=plan.id,
            panel_username="u_b",
            status=ServiceStatus.ACTIVE,
            data_limit=GB,
        )
    )
    await session.commit()
    await send(callback_update(OWNER, Admin(section="broadcast").pack()))
    await send(message_update(OWNER, "Big sale"))
    row = await session.scalar(select(Broadcast).order_by(Broadcast.id.desc()))
    assert row is not None
    await send(callback_update(OWNER, Admin(section="bc", action="aud", id=row.id, page=2).pack()))  # active
    await send(callback_update(OWNER, Admin(section="bc", action="pin", id=row.id).pack()))
    await send(callback_update(OWNER, Admin(section="bc", action="button", id=row.id).pack()))
    await send(message_update(OWNER, "Buy | javascript:alert(1)"))
    assert FA("adm_bc_button_invalid") in fake.texts()
    await send(message_update(OWNER, "Buy now | https://t.me/PauruBot"))
    await session.refresh(row)
    assert (
        row.audience == {"kind": "active", "pin": True, "pin_hours": 0}
        and row.buttons[0]["url"] == "https://t.me/PauruBot"
    )
    await send(callback_update(OWNER, Admin(section="bc", action="send", id=row.id).pack()))
    fake.reset()
    await drain(session, "broadcast")
    copies = [c for c in fake.calls if type(c).__name__ == "CopyMessage"]
    assert [c.chat_id for c in copies] == [buyer_uid]
    assert copies[0].reply_markup.inline_keyboard[0][0].url == "https://t.me/PauruBot"
    assert any(type(c).__name__ == "PinChatMessage" for c in fake.calls)


async def test_delete_needs_the_issued_confirmation(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel, plan = env["send"], env["fake"], env["shop"], env["panel"], env["plan"]
    await send(message_update(OWNER, "/start"))
    customer = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        plan_id=plan.id,
        panel_username="u_del",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * GB,
        expire_at=utcnow() + timedelta(days=5),
    )
    session.add(service)
    await session.commit()
    env["fake_panel"].users["u_del"] = {
        "username": "u_del",
        "status": "active",
        "used_traffic": 0,
        "data_limit": 10 * GB,
        "expire": int(service.expire_at.timestamp()),
        "sub": "s1",
    }
    # The old, guessable confirm button (page=1) no longer deletes anything.
    await send(callback_update(OWNER, Admin(section="svcact", action="del", id=service.id, page=1).pack()))
    await session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE

    await send(callback_update(OWNER, Admin(section="svcact", action="del", id=service.id).pack()))
    confirmation = fake.buttons(fake.last(EditMessageText))[0][1]
    assert confirmation is not None
    unpacked = Admin.unpack(confirmation)
    assert unpacked.action == "del" and unpacked.page > 1
    await send(callback_update(OWNER, confirmation))
    await session.refresh(service)
    assert service.status == ServiceStatus.DELETED
