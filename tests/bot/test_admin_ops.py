"""Running the shop from the bot (OPS-09): status, backups and log groups."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

import pytest
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from aiogram.types import Chat, Message, Update
from pydantic import SecretStr
from sqlalchemy import select

from paurubot.auth.staff import create_staff
from paurubot.bot.app import build_dispatcher
from paurubot.bot.callbacks import Admin
from paurubot.config import get_settings
from paurubot.db.models import Job, LogChannel, StaffRole
from paurubot.i18n import Texts
from paurubot.notifications import telegram as notify_telegram
from tests import factories
from tests.fakes.telegram import callback_update, make_bot, message_update, user

FA = Texts("fa")
OWNER = 7801
SUPPORT = 7802
_ids = count(900_000)


@pytest.fixture
async def env(session, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    settings = get_settings()
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "backups"))
    monkeypatch.setattr(settings, "backup_passphrase", SecretStr("a-long-backup-passphrase"))
    shop = await factories.shop(session, "Ops shop")
    await create_staff(
        session, shop.id, role=StaffRole.OWNER, username="ops_owner", password=None, telegram_id=OWNER
    )
    await create_staff(
        session, shop.id, role=StaffRole.SUPPORT, username="ops_support", password=None, telegram_id=SUPPORT
    )
    await session.commit()
    bot, fake = make_bot()
    notify_telegram.set_bot(bot)
    dp = build_dispatcher(MemoryStorage(), shop_id=shop.id)

    async def send(update):  # type: ignore[no-untyped-def]
        await dp.feed_update(bot, update)

    yield {"shop": shop, "fake": fake, "send": send, "dir": tmp_path / "backups"}
    notify_telegram.set_bot(None)


def group_update(uid: int, text: str, *, thread: int | None = None) -> Update:
    msg = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=Chat(id=-100777, type="supergroup", is_forum=thread is not None),
        from_user=user(uid),
        text=text,
        message_thread_id=thread,
        is_topic_message=thread is not None or None,
    )
    return Update(update_id=next(_ids), message=msg)


async def test_status_lists_checks(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake = env["send"], env["fake"]
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="status").pack()))
    text = fake.last(EditMessageText).text
    assert "postgresql" in text and "redis" in text


async def test_backups_list_send_and_queue(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, folder = env["send"], env["fake"], env["dir"]
    folder.mkdir(parents=True)
    (folder / "paurubot-20261001-120000.pbak").write_bytes(b"x" * 100)
    (folder / "../outside.pbak").write_bytes(b"secret")
    await send(message_update(OWNER, "/start"))
    await send(callback_update(OWNER, Admin(section="backups").pack()))
    listing = fake.last(EditMessageText).text
    assert "paurubot-20261001-120000.pbak" in listing and "outside" not in listing
    await send(callback_update(OWNER, Admin(section="backups", action="get", page=0).pack()))
    doc = fake.last(SendDocument)
    assert doc.document.filename == "paurubot-20261001-120000.pbak" and doc.protect_content is True
    await send(callback_update(OWNER, Admin(section="backups", action="get", page=7).pack()))  # forged index
    assert len([c for c in fake.calls if isinstance(c, SendDocument)]) == 1
    await send(callback_update(OWNER, Admin(section="backups", action="now").pack()))
    job = await session.scalar(select(Job).where(Job.queue == "ops", Job.kind == "backup"))
    assert job is not None
    # Support staff can't reach backups.
    fake.reset()
    await send(message_update(SUPPORT, "/start"))
    await send(callback_update(SUPPORT, Admin(section="backups", action="get", page=0).pack()))
    assert not [c for c in fake.calls if isinstance(c, SendDocument)]


async def test_setlog_in_a_forum_topic(session, env) -> None:  # type: ignore[no-untyped-def]
    send, fake, shop = env["send"], env["fake"], env["shop"]
    await send(message_update(OWNER, "/start"))
    await send(group_update(OWNER, "/setlog orders", thread=55))
    row = await session.scalar(
        select(LogChannel).where(LogChannel.shop_id == shop.id, LogChannel.topic == "orders")
    )
    assert row is not None and (row.chat_id, row.thread_id) == (-100777, 55)
    assert FA("adm_setlog_done", topic="orders") in fake.texts()
    await send(group_update(OWNER, "/setlog nonsense"))
    assert any("/setlog" in text for text in fake.texts())
    await send(group_update(OWNER, "/logs"))
    assert FA("adm_logs_here", topics="orders") in fake.texts()
    # Someone who isn't staff can't point reports at a group.
    await send(group_update(7899, "/setlog payments"))
    assert await session.scalar(select(LogChannel).where(LogChannel.topic == "payments")) is None
    await send(group_update(OWNER, "/unsetlog orders"))
    assert await session.scalar(select(LogChannel).where(LogChannel.topic == "orders")) is None
    assert isinstance(fake.last(SendMessage), SendMessage)
