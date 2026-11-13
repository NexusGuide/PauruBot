"""Retry only transient GetUpdates failures, without flooding the operational channel."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import TelegramType

from paurubot.logs import log_tags

log = logging.getLogger(__name__)
ESCALATE_AFTER = 300.0
REPEAT_ALERT_AFTER = 900.0
# A token Telegram rejects (revoked in BotFather, or the bot deleted) is not fixed by retrying
# quickly: aiogram did it every few seconds, an error in the log channel each time.
UNAUTHORIZED_RETRY = 600.0
UNAUTHORIZED_ALERT_EVERY = 3600.0


class PollingRetryMiddleware(BaseRequestMiddleware):
    """Keep aiogram's polling/offset machinery; retry transport errors at the request boundary.

    Conflict and bad-request errors still reach aiogram unchanged. A rejected token waits
    quietly, retried every ten minutes and reported once an hour, until it works again or the
    bot is replaced. Other Telegram methods must retain their caller's retry/idempotency policy.
    """

    def __init__(self, shop_id: int | None = None) -> None:
        self.shop_id = shop_id

    def _log(self, level: int, message: str, *args: object) -> None:
        token = log_tags.set({"shop": self.shop_id})
        try:
            log.log(level, message, *args)
        finally:
            log_tags.reset(token)

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Any:
        request_method = method  # retain the generic method type across the GetUpdates check
        if not isinstance(method, GetUpdates):
            return await make_request(bot, method)
        began = time.monotonic()
        attempts = 0
        next_alert = ESCALATE_AFTER
        rejected_alert = 0.0
        while True:
            try:
                result = await make_request(bot, request_method)
            except TelegramUnauthorizedError:
                attempts += 1
                elapsed = time.monotonic() - began
                if elapsed >= rejected_alert:
                    self._log(
                        logging.ERROR,
                        "Telegram rejected the bot token (shop %s): it was revoked or the bot deleted. "
                        "Connect the bot again with a new token; retrying every %.0f min",
                        self.shop_id if self.shop_id is not None else "main",
                        UNAUTHORIZED_RETRY / 60,
                    )
                    rejected_alert = elapsed + UNAUTHORIZED_ALERT_EVERY
                await asyncio.sleep(UNAUTHORIZED_RETRY)
                continue
            except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as exc:
                attempts += 1
                elapsed = time.monotonic() - began
                # Positive jitter never undercuts Telegram's retry_after minimum.
                delay = min(30.0, 2.0 ** min(attempts - 1, 5)) * random.uniform(1.0, 1.2)
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, float(exc.retry_after))
                self._log(
                    logging.WARNING,
                    "Telegram polling transient %s; attempt=%s retry_in=%.1fs",
                    type(exc).__name__,
                    attempts,
                    delay,
                )
                if elapsed >= next_alert:
                    self._log(
                        logging.ERROR,
                        "Telegram polling unavailable for %.0fs (%s attempts); retries continue",
                        elapsed,
                        attempts,
                    )
                    next_alert = elapsed + REPEAT_ALERT_AFTER
                await asyncio.sleep(delay)  # cancellation must immediately stop this retry loop
                continue
            if attempts:
                self._log(
                    logging.INFO,
                    "Telegram polling recovered after %.0fs (%s retries)",
                    time.monotonic() - began,
                    attempts,
                )
            return result
