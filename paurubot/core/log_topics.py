"""Forum supergroup report topic auto-setup and delivery test helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot.core import settings as shop_settings
from paurubot.db.models import LogChannel
from paurubot.notifications import log_reports, telegram

log = logging.getLogger(__name__)

TOPIC_ICONS: dict[str, str] = {
    "orders": "🛒",
    "payments": "💳",
    "receipts": "🧾",
    "users": "👤",
    "errors": "🚨",
    "services": "⚙️",
    "crons": "⏱",
    "reports": "📊",
    "backups": "💾",
    "resellers": "🤝",
    "panels": "🖥",
    "tickets": "🎫",
    "trials": "🧪",
    "referrals": "🎁",
    "other": "📌",
}


class TopicSetupError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def auto_setup_forum_topics(session: AsyncSession, shop_id: int, chat_id: int) -> dict[str, list[str]]:
    """Create missing Telegram forum topics in `chat_id` and bind each to LogChannel."""
    bot = await telegram.bot_for(shop_id)
    if bot is None:
        raise TopicSetupError("bot_unavailable")
    try:
        chat = await bot.get_chat(chat_id)
    except Exception as exc:
        raise TopicSetupError("chat_unreachable") from exc
    if not getattr(chat, "is_forum", False):
        raise TopicSetupError("group_not_forum")

    t = await log_reports.shop_texts(session, shop_id)
    existing = {
        row.topic: row
        for row in await session.scalars(select(LogChannel).where(LogChannel.shop_id == shop_id))
    }
    created: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    for topic in shop_settings.REPORT_TOPICS:
        cur = existing.get(topic)
        if cur is not None and cur.chat_id == chat_id and cur.thread_id is not None:
            skipped.append(topic)
            continue
        title = f"{TOPIC_ICONS.get(topic, '📌')} {t(f'log_topic_{topic}')}"[:120]
        try:
            forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=title)
            thread_id = int(forum_topic.message_thread_id)
        except Exception:
            log.warning("failed creating forum topic %s in chat %s", topic, chat_id, exc_info=True)
            failed.append(topic)
            continue
        if cur is None:
            session.add(LogChannel(shop_id=shop_id, topic=topic, chat_id=chat_id, thread_id=thread_id))
        else:
            cur.chat_id = chat_id
            cur.thread_id = thread_id
        created.append(topic)
        await asyncio.sleep(0.15)

    await session.flush()
    return {"created": created, "skipped": skipped, "failed": failed}


async def send_topic_test(session: AsyncSession, shop_id: int, row: LogChannel) -> dict[str, Any]:
    """Send a short test message to verify bot permissions in a configured log channel/topic."""
    t = await log_reports.shop_texts(session, shop_id)
    label = t(f"log_topic_{row.topic}")
    text = t("report_topic_test", topic=label, code=row.topic)
    msg = await telegram.send_text(
        row.chat_id,
        text,
        message_thread_id=row.thread_id,
        shop_id=shop_id,
    )
    if msg is None:
        raise TopicSetupError("bot_unavailable")
    return {"ok": True, "message_id": msg.message_id, "topic": row.topic}
