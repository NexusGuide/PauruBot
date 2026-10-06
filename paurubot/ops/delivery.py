"""Send finished backups to Telegram (OPS-06)."""

from __future__ import annotations

import logging

from aiogram.types import FSInputFile
from sqlalchemy import select

from paurubot.core.formatting import size
from paurubot.core.shops import root_shop
from paurubot.db.models import LogChannel
from paurubot.db.session import session_scope
from paurubot.i18n import Texts
from paurubot.notifications import telegram
from paurubot.notifications.handlers import staff_chats
from paurubot.ops.backup import BackupInfo

log = logging.getLogger(__name__)

TELEGRAM_FILE_LIMIT = 49 * 1024 * 1024


async def send_backup(info: BackupInfo) -> int:
    """Post the archive to the backups channel, or to every owner when no channel is set."""
    async with session_scope() as session:
        shop = await root_shop(session)
        if shop is None:
            return 0
        channel = await session.scalar(
            select(LogChannel).where(LogChannel.shop_id == shop.id, LogChannel.topic == "backups")
        )
        targets: list[tuple[int, int | None]] = (
            [(channel.chat_id, channel.thread_id)]
            if channel is not None
            else [(chat, None) for chat in await staff_chats(session, shop.id, "backups.manage")]
        )
    texts = Texts("fa")
    sent = 0
    for chat_id, thread_id in targets:
        try:
            if info.size > TELEGRAM_FILE_LIMIT:
                await telegram.send_text(
                    chat_id,
                    texts("backup_too_large", name=info.name, size=size(info.size)),
                    message_thread_id=thread_id,
                )
            else:
                await telegram.send_document(
                    chat_id,
                    FSInputFile(info.path, filename=info.name),
                    caption=texts("backup_caption", name=info.name, size=size(info.size)),
                    message_thread_id=thread_id,
                )
            sent += 1
        except telegram.Undeliverable:
            log.warning("backup could not be delivered to %s", chat_id)
    return sent
