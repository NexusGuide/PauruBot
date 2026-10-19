"""Recoverable error batching; Telegram failures belong to the durable notify queue."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paurubot import kv
from paurubot.config import get_settings
from paurubot.core.formatting import esc
from paurubot.db.models import LogChannel, Shop
from paurubot.db.session import session_scope
from paurubot.logs import TelegramErrorHandler, error_shop_id
from paurubot.notifications import telegram
from paurubot.security.redact import redact
from paurubot.worker import queue
from paurubot.worker.runner import Permanent

log = logging.getLogger(__name__)
BATCH_KEY = "errors:inflight"
# Capture one stable batch before removing pending records. Concurrent/restarted flushes
# replay the same id; durable enqueue dedupe and compare-and-delete make replay harmless.
_TAKE = """
local batch = redis.call('get', KEYS[2])
if batch then return batch end
local rows = redis.call('lrange', KEYS[1], 0, 49)
if #rows == 0 then return nil end
batch = cjson.encode({id=ARGV[1], rows=rows})
redis.call('set', KEYS[2], batch)
redis.call('ltrim', KEYS[1], 50, -1)
return batch
"""
_ACK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def _channel(session: AsyncSession, shop_id: int) -> LogChannel | None:
    return await session.scalar(
        select(LogChannel)
        .where(LogChannel.shop_id == shop_id, LogChannel.topic.in_(["errors", "all"]))
        .order_by(LogChannel.topic != "errors", LogChannel.id)
    )


async def _destinations(
    session: AsyncSession, shop_id: int | None
) -> list[tuple[int | None, int, int | None]]:
    if shop_id is not None and await session.get(Shop, shop_id) is not None:
        channel = await _channel(session, shop_id)
        return [(shop_id, channel.chat_id, channel.thread_id)] if channel else []
    return [(None, chat_id, None) for chat_id in sorted(set(get_settings().owner_telegram_ids))]


async def flush() -> None:
    redis = kv.get_redis()
    raw = await redis.eval(_TAKE, 2, TelegramErrorHandler.KEY, BATCH_KEY, uuid.uuid4().hex)
    if not raw:
        return
    batch = json.loads(raw)
    groups: dict[tuple[int | None, str, str], list[dict[str, Any]]] = {}
    for item in batch["rows"]:
        try:
            entry = json.loads(item)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        name, message = entry.get("logger", ""), entry.get("msg", "")
        if not isinstance(name, str) or not isinstance(message, str):
            continue
        scope = error_shop_id(entry.get("shop_id"))
        groups.setdefault((scope, name, message), []).append(entry)
    async with session_scope() as session:
        for index, ((scope, name, message), entries) in enumerate(groups.items()):
            first = entries[0]
            text = (
                f"⚠️ <b>{esc(redact(str(first.get('process') or '?')))}</b> · {esc(redact(name))}"
                + (f" ×{len(entries)}" if len(entries) > 1 else "")
                + f"\n{esc(redact(message))}"
            )
            if first.get("cid"):
                text += f"\n<code>{esc(redact(str(first['cid'])))}</code>"
            if first.get("exc"):
                text += f"\n<pre>{esc(redact(str(first['exc']))[-900:])}</pre>"
            for shop_id, chat_id, thread_id in await _destinations(session, scope):
                await queue.enqueue(
                    session,
                    "notify",
                    "error_report",
                    {"chat_id": chat_id, "thread_id": thread_id, "text": text},
                    shop_id=shop_id,
                    dedupe_key=f"error:{batch['id']}:{index}:{shop_id}:{chat_id}:{thread_id}",
                )
    # Only ACK after commit. A crash/Redis failure here replays durable enqueue, not delivery.
    await redis.eval(_ACK, 1, BATCH_KEY, raw)


async def deliver(session: AsyncSession, job: queue.Claimed) -> None:
    """Recheck current destination authorization; never reroute a stale error payload."""
    chat_id, thread_id, text = (
        job.payload.get("chat_id"),
        job.payload.get("thread_id"),
        job.payload.get("text"),
    )
    if (
        type(chat_id) is not int
        or not isinstance(text, str)
        or (thread_id is not None and type(thread_id) is not int)
    ):
        raise Permanent("invalid error report payload")
    if job.shop_id is None:
        allowed = chat_id in get_settings().owner_telegram_ids and thread_id is None
    else:
        channel = await _channel(session, job.shop_id)
        allowed = channel is not None and (channel.chat_id, channel.thread_id) == (chat_id, thread_id)
    if not allowed:
        raise Permanent("error report destination is no longer configured")
    try:
        await telegram.send_text(chat_id, text, message_thread_id=thread_id, shop_id=job.shop_id)
    except (telegram.Undeliverable, TelegramBadRequest) as exc:
        # Invalid destinations/HTML cannot be repaired by retries. Keep a dead row, not
        # another Telegram alert about the failed alert. Normal process logs remain intact.
        log.warning("error report delivery permanently rejected: %s", type(exc).__name__)
        raise Permanent("error report permanently rejected") from exc
