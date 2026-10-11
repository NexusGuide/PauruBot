"""Admin hub in the bot: feature switches, customer lists and period stats (ADM-12)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendMessage
from sqlalchemy import update

import paurubot.notifications.handlers  # noqa: F401  (registers the queues)
from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.bot.routers import admin_hub
from paurubot.core import settings as shop_settings
from paurubot.db.models import Customer, Job, JobStatus, StaffRole
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update

FA = Texts("fa")
OWNER = 7701
SUPPORT = 7702


def test_feature_keys_are_public_bools_and_labelled() -> None:
    assert len(admin_hub.FEATURE_KEYS) == len(set(admin_hub.FEATURE_KEYS))
    for key in admin_hub.FEATURE_KEYS:
        assert not shop_settings.SPECS[key].secret, key
    for locale in ("fa", "en"):
        t = Texts(locale)
        keys = [f"adm_feat_{k}" for k in admin_hub.FEATURE_KEYS]
        keys += [f"adm_feat_group_{g}" for g in admin_hub.FEATURES]
        keys += [f"adm_ulist_{f}" for f in admin_hub.USER_FILTERS]
        keys += [f"adm_pstats_{p}" for p in admin_hub.PERIODS]
        for key in keys:
            assert t(key) != key, (locale, key)


def test_period_ranges_follow_tehran_days() -> None:
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # 15:30 in Tehran
    start, end = admin_hub.period_range("today", now)
    assert start == datetime(2026, 10, 10, 20, 30, tzinfo=UTC) and end is None
    start, end = admin_hub.period_range("yesterday", now)
    assert start == datetime(2026, 10, 9, 20, 30, tzinfo=UTC)
    assert end == datetime(2026, 10, 10, 20, 30, tzinfo=UTC)
    assert admin_hub.period_range("all", now) == (None, None)


def test_gateway_needs_credentials_before_switching_on() -> None:
    assert admin_hub.can_enable("lottery_enabled", {})
    if "plisio_api_key" in shop_settings.SPECS:
        assert not admin_hub.can_enable("plisio_enabled", {"plisio_api_key": ""})
        assert admin_hub.can_enable("plisio_enabled", {"plisio_api_key": "k"})


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Admin hub")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="hub_owner", password=None, telegram_id=OWNER
    )
    await create_staff(
        session, shop.id, role=StaffRole.SUPPORT, username="hub_support", password=None, telegram_id=SUPPORT
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


def labels(fake, kind=EditMessageText):  # type: ignore[no-untyped-def]
    return [text for text, _ in fake.buttons(fake.last(kind))]


async def test_home_has_hub_buttons(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, FA("btn_admin")))
    owner = labels(fake, SendMessage)
    for key in ("adm_hub_features", "adm_hub_users", "adm_hub_stats"):
        assert FA(key) in owner, key


async def test_feature_toggle_and_permission(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(OWNER, "/start"))
    cfg = await shop_settings.load(session, shop.id, fresh=True)
    for key in admin_hub.FEATURE_KEYS:
        assert isinstance(cfg[key], bool), key
    index = admin_hub.FEATURE_KEYS.index("lottery_enabled")
    before = cfg["lottery_enabled"]
    await send(callback_update(OWNER, Admin(section="feat", action="tog", id=index).pack()))
    assert (await shop_settings.load(session, shop.id, fresh=True))["lottery_enabled"] is (not before)
    # Support staff cannot change settings, even with a forged button.
    await send(message_update(SUPPORT, "/start"))
    fake.reset()
    await send(callback_update(SUPPORT, Admin(section="feat", action="tog", id=index).pack()))
    assert (await shop_settings.load(session, shop.id, fresh=True))["lottery_enabled"] is (not before)
    assert not [c for c in fake.calls if isinstance(c, EditMessageText)]


async def test_user_list_filters(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(OWNER, "/start"))
    banned = await factories.customer(session, shop, username="bad_one")
    await factories.customer(session, shop, username="good_one")
    await session.execute(update(Customer).where(Customer.id == banned.id).values(is_banned=True))
    await session.commit()
    await send(callback_update(OWNER, Admin(section="ulist").pack()))
    assert FA("adm_ulist_banned") in labels(fake)
    index = admin_hub.USER_FILTERS.index("banned")
    await send(callback_update(OWNER, Admin(section="ulist", action="f", id=index).pack()))
    shown = labels(fake)
    assert any("bad_one" in text for text in shown)
    assert not any("good_one" in text for text in shown)


async def test_period_stats(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="pstats", page=admin_hub.PERIODS.index("all")).pack()))
    shown = labels(fake)
    assert FA("adm_pstats_today") in shown
    assert "\u2705 " + FA("adm_pstats_all") in shown
