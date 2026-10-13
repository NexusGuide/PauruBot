"""Best-effort redelivery guard for Telegram updates (F-03).

Only completed handlers are remembered for 24 hours. An in-flight update holds a short,
owner-checked lease instead: process death cannot turn it into a completed update. Concurrent
webhooks wait or fail so Telegram retries, rather than acknowledging unfinished work.
Synthetic menu updates (id 0) are exempt. Financial operations retain their own idempotency;
Redis and the database cannot provide an atomic exactly-once transaction here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from paurubot import kv

log = logging.getLogger(__name__)
TTL_SECONDS = 24 * 3600
LEASE_SECONDS = 300
# Cancel a stuck handler before its lease expires and another delivery can start.
HANDLER_TIMEOUT_SECONDS = 240


def update_key(bot_id: int, update_id: int) -> str:
    return f"upd:{bot_id}:{update_id}"


class UpdateDedupeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        bot = data.get("bot")
        update_id = event.update_id if isinstance(event, Update) else 0
        if not update_id or bot is None:
            return await handler(event, data)
        key = update_key(bot.id, update_id)
        async with AsyncExitStack() as stack:
            try:
                owner = await stack.enter_async_context(
                    kv.lock(key, ttl=LEASE_SECONDS, wait=True, wait_seconds=10.0)
                )
                if await kv.seen_once(key):
                    log.info("duplicate update skipped", extra={"bot_id": bot.id, "update_id": update_id})
                    return None
            except kv.LockNotAcquired:
                # Do not ACK a concurrent delivery while the first handler is still unfinished.
                raise
            except Exception:
                log.warning("update dedupe unavailable", exc_info=True)
                return await handler(event, data)
            async with asyncio.timeout(HANDLER_TIMEOUT_SECONDS):
                result = await handler(event, data)
            # Never run the handler again here on a Redis failure after its successful completion.
            await kv.complete_once(key, owner, TTL_SECONDS)
            return result
