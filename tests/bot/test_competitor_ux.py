"""Competitor UX gaps: quick admin commands, callback lock (NEW-06..NEW-11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage
from sqlalchemy import update

import paurubot.notifications.handlers  # noqa: F401  (registers the queues)
from paurubot import kv
from paurubot.auth.staff import create_staff
from paurubot.bot import keyboards
from paurubot.bot.app import build_dispatcher
from paurubot.bot.routers.admin_tools import parse_service_command, parse_stats_arg, stats_window
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, Service, ServiceStatus, StaffRole
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import make_bot, message_update

FA = Texts("fa")
OWNER = 7601


@pytest.fixture
async def env(session):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).where(Job.status == JobStatus.QUEUED).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Competitor UX")
    panel = await factories.panel(session, shop)
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="ux_owner", password=None, telegram_id=OWNER
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "panel": panel, "fake": fake, "send": send}
    notify_telegram.set_bot(None)


def test_parse_service_command() -> None:
    assert parse_service_command("/config 123") == ("config", 123)
    assert parse_service_command("/extend 45") == ("extend", 45)
    assert parse_service_command("/config") == ("config", None)
    assert parse_service_command("/config abc") == ("config", None)
    assert parse_service_command("/lookup some_user") == ("lookup", None)
    assert parse_service_command(None) == ("", None)


def test_parse_stats_arg() -> None:
    assert parse_stats_arg(None) == "today"
    assert parse_stats_arg("") == "today"
    assert parse_stats_arg("1h") == "1h"
    assert parse_stats_arg("/today") == "today"
    assert parse_stats_arg("yesterday") == "yesterday"
    assert parse_stats_arg("month") == "month"
    assert parse_stats_arg("this-month") == "month"
    assert parse_stats_arg("nonsense") is None


def test_stats_window_ranges() -> None:
    now = datetime(2026, 3, 21, 12, 0, tzinfo=UTC)
    since, until = stats_window("1h", now)
    assert (since, until) == (now - timedelta(hours=1), None)
    since, until = stats_window("today", now)
    assert since == datetime(2026, 3, 21, tzinfo=UTC) and until is None
    since, until = stats_window("yesterday", now)
    assert since == datetime(2026, 3, 20, tzinfo=UTC)
    assert until == datetime(2026, 3, 21, tzinfo=UTC)
    since, until = stats_window("month", now)
    assert since == datetime(2026, 3, 1, tzinfo=UTC) and until is None


class _Chat:
    id = 55


class _Msg:
    chat = _Chat()
    message_id = 77


class _User:
    id = 99


class _Call:
    from_user = _User()
    message = _Msg()

    def __init__(self) -> None:
        self.answers: list[str | None] = []

    async def answer(self, text: str | None = None, *args: object, **kwargs: object) -> None:
        self.answers.append(text)


async def test_callback_lock_blocks_double_tap() -> None:
    t = Texts("fa")
    first, second = _Call(), _Call()
    assert await keyboards.try_callback_lock(first, t) is True  # type: ignore[arg-type]
    assert await keyboards.try_callback_lock(second, t) is False  # type: ignore[arg-type]
    assert second.answers and t("callback_busy") in second.answers
    other_message = _Call()
    other_message.message = type("M", (), {"chat": _Chat(), "message_id": 78})()
    assert await keyboards.try_callback_lock(other_message, t) is True  # type: ignore[arg-type]


async def test_callback_lock_key_is_per_user_and_message() -> None:
    assert keyboards.callback_lock_key(1, 2, 3) != keyboards.callback_lock_key(1, 2, 4)
    assert keyboards.callback_lock_key(1, 2, 3) != keyboards.callback_lock_key(9, 2, 3)
    assert keyboards.callback_lock_key(1, None, None) == "tap:1:0:0"


async def test_set_once_survives_without_redis(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def boom(*args: object, **kwargs: object) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(kv, "set_once", boom)
    assert await keyboards.try_callback_lock(_Call(), Texts("fa")) is True  # type: ignore[arg-type]


async def test_config_and_extend_commands(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel = env["send"], env["fake"], env["shop"], env["panel"]
    customer = await factories.customer(session, shop)
    service = Service(
        shop_id=shop.id,
        customer_id=customer.id,
        panel_id=panel.id,
        panel_username="u_quick",
        label="Laptop",
        status=ServiceStatus.ACTIVE,
        data_limit=10 * 1024**3,
    )
    session.add(service)
    await session.commit()
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, f"/config {service.id}"))
    assert any("Laptop" in text for text in fake.texts(OWNER))
    await send(message_update(OWNER, f"/extend {service.id}"))
    last = fake.last(SendMessage)
    assert FA("adm_extend_pick") in (last.text or "")
    assert FA("adm_svc_d30") in [text for text, _ in fake.buttons(last)]
    await send(message_update(OWNER, "/config"))
    assert FA("adm_config_usage") in fake.texts(OWNER)[-1]
    await send(message_update(OWNER, "/config 999999"))
    assert FA("adm_service_not_found") in fake.texts(OWNER)[-1]


async def test_lookup_and_stats_commands(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop, panel = env["send"], env["fake"], env["shop"], env["panel"]
    customer = await factories.customer(session, shop)
    session.add(
        Service(
            shop_id=shop.id,
            customer_id=customer.id,
            panel_id=panel.id,
            panel_username="foreign_user",
            label="Phone",
            status=ServiceStatus.ACTIVE,
        )
    )
    await session.commit()
    await send(message_update(OWNER, "/start"))
    await send(message_update(OWNER, "/lookup foreign_user"))
    assert any("Phone" in text for text in fake.texts(OWNER))
    await send(message_update(OWNER, "/lookup"))
    assert FA("adm_lookup_usage") in fake.texts(OWNER)[-1]
    await send(message_update(OWNER, "/stats 1h"))
    assert FA("adm_stats_1h") in fake.texts(OWNER)[-1]
    await send(message_update(OWNER, "/stats nonsense"))
    assert FA("adm_stats_usage") in fake.texts(OWNER)[-1]
    assert utcnow() is not None
