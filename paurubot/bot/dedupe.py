"""Process each Telegram update once (F-03).

Telegram re-delivers a webhook update when our answer is slow or fails, and a restart can replay
the last polling batch. Money paths are already idempotent; this guards everything else (tickets,
revokes, messages). Synthetic updates fed by the inline menu use update_id 0 and are never deduped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update

from paurubot import kv

log = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600


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
        try:
            first = await kv.set_once(key, TTL_SECONDS)
        except Exception:  # Redis trouble must not swallow updates: process rather than drop
            log.warning("update dedupe unavailable", exc_info=True)
            return await handler(event, data)
        if not first:
            log.info("duplicate update skipped", extra={"bot_id": bot.id, "update_id": update_id})
            return None
        try:
            return await handler(event, data)
        except Exception:
            # A failed attempt may be retried by Telegram; let that retry through.
            try:
                await kv.forget_once(key)
            except Exception:
                log.warning("could not release update dedupe key", exc_info=True)
            raise
