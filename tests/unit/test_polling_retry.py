"""Request-boundary polling retries: no live Telegram, no real sleeps."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.methods import GetUpdates, SendMessage

from paurubot.bot import polling
from paurubot.logs import log_tags
from paurubot.notifications.telegram import make_bot


@pytest.fixture
def clock(monkeypatch):  # type: ignore[no-untyped-def]
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay

    # Patch module references, not the global time module used by asyncio.
    from types import SimpleNamespace

    monkeypatch.setattr(polling, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(polling, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(polling.random, "uniform", lambda _a, _b: 1.0)
    return now, sleeps


async def test_retry_after_floor_then_network_and_server_recover_without_error_alert(clock, caplog) -> None:  # type: ignore[no-untyped-def]
    method = GetUpdates(offset=42)
    request = AsyncMock(
        side_effect=[
            TelegramRetryAfter(method=method, message="429 token=do-not-log", retry_after=45),
            TelegramServerError(method=method, message="Bad Gateway"),
            TelegramNetworkError(method=method, message="network"),
            [],
        ]
    )
    bot = make_bot()
    try:
        with caplog.at_level(logging.INFO, logger=polling.__name__):
            result = await polling.PollingRetryMiddleware()(request, bot, method)
    finally:
        await bot.session.close()
    assert result == [] and request.await_count == 4
    assert clock[1] == [45.0, 2.0, 4.0]
    assert method.offset == 42
    assert all(call.args[1] is method for call in request.await_args_list)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)
    assert "recovered" in caplog.text and "do-not-log" not in caplog.text


async def test_persistent_outage_escalates_at_threshold_not_on_each_retry(clock, caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    method = GetUpdates()
    request = AsyncMock(
        side_effect=[
            *[TelegramRetryAfter(method=method, message="rate", retry_after=100) for _ in range(14)],
            [],
        ]
    )
    scopes = []
    original = polling.log.log

    def capture(level, message, *args):  # type: ignore[no-untyped-def]
        if level == logging.ERROR:
            scopes.append(log_tags.get())
        original(level, message, *args)

    monkeypatch.setattr(polling.log, "log", capture)
    bot = make_bot()
    before = log_tags.set({"shop": 999})
    try:
        with caplog.at_level(logging.INFO, logger=polling.__name__):
            await polling.PollingRetryMiddleware(123)(request, bot, method)
        assert log_tags.get() == {"shop": 999}
    finally:
        log_tags.reset(before)
        await bot.session.close()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2  # at 300s and 1200s, not every 100s
    assert scopes == [{"shop": 123}, {"shop": 123}]
    assert "recovered" in caplog.text


@pytest.mark.parametrize("exception", [TelegramBadRequest, TelegramConflictError])
async def test_fatal_polling_errors_are_not_swallowed(exception, clock) -> None:  # type: ignore[no-untyped-def]
    method = GetUpdates()
    error = exception(method=method, message="fatal")
    request = AsyncMock(side_effect=error)
    bot = make_bot()
    try:
        with pytest.raises(exception):
            await polling.PollingRetryMiddleware()(request, bot, method)
    finally:
        await bot.session.close()
    request.assert_awaited_once()
    assert clock[1] == []


async def test_a_rejected_token_waits_quietly_instead_of_flooding_the_log(clock, caplog, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A reseller bot whose token was revoked: aiogram retried it every few seconds, each attempt an
    error in the log channel. Now it is retried slowly and told once a day to the reports group,
    by the main bot, naming the bot and its owner (see test_bot_token_rejected)."""
    reported: list[int] = []

    async def report(shop_id: int) -> None:
        reported.append(shop_id)

    monkeypatch.setattr(polling, "report_rejected", report)
    method = GetUpdates()
    rejected = TelegramUnauthorizedError(method=method, message="Unauthorized")
    request = AsyncMock(side_effect=[rejected] * 12 + [[]])
    bot = make_bot()
    try:
        with caplog.at_level(logging.INFO, logger=polling.__name__):
            result = await polling.PollingRetryMiddleware(shop_id=7)(request, bot, method)
    finally:
        await bot.session.close()
    assert result == [] and request.await_count == 13
    assert clock[1] == [polling.UNAUTHORIZED_RETRY] * 12
    assert reported == [7], "two hours of retries: one report"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], "nothing in the error channel"


async def test_the_main_bots_rejected_token_is_an_error_once(clock, caplog) -> None:  # type: ignore[no-untyped-def]
    method = GetUpdates()
    rejected = TelegramUnauthorizedError(method=method, message="Unauthorized")
    request = AsyncMock(side_effect=[rejected] * 3 + [[]])
    bot = make_bot()
    try:
        with caplog.at_level(logging.INFO, logger=polling.__name__):
            await polling.PollingRetryMiddleware()(request, bot, method)
    finally:
        await bot.session.close()
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1


async def test_non_polling_methods_are_never_retried(clock) -> None:  # type: ignore[no-untyped-def]
    method = SendMessage(chat_id=123, text="not safe to repeat blindly")
    request = AsyncMock(side_effect=TelegramServerError(method=method, message="502"))
    bot = make_bot()
    try:
        with pytest.raises(TelegramServerError):
            await polling.PollingRetryMiddleware()(request, bot, method)
    finally:
        await bot.session.close()
    request.assert_awaited_once()
    assert clock[1] == []


async def test_cancellation_interrupts_retry_sleep(clock, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    async def cancel(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(polling.asyncio, "sleep", cancel)
    method = GetUpdates()
    request = AsyncMock(side_effect=TelegramServerError(method=method, message="502"))
    bot = make_bot()
    try:
        with pytest.raises(asyncio.CancelledError):
            await polling.PollingRetryMiddleware()(request, bot, method)
    finally:
        await bot.session.close()
    request.assert_awaited_once()


async def test_factory_installs_polling_retry_for_main_and_reseller() -> None:
    for scope in (None, 123):
        bot = make_bot(shop_id=scope)
        try:
            middlewares = list(bot.session.middleware)
            retry = [m for m in middlewares if isinstance(m, polling.PollingRetryMiddleware)]
            assert len(retry) == 1 and retry[0].shop_id == scope
        finally:
            await bot.session.close()


async def test_cli_reseller_factory_passes_each_shop_scope(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock

    from paurubot.bot import app
    from paurubot.cli import commands
    from paurubot.core import resale
    from paurubot.db import session as db_session
    from paurubot.notifications import telegram
    from paurubot.security.tokens import sha256_hex

    rows = [(SimpleNamespace(id=101), "synthetic-token-1"), (SimpleNamespace(id=202), "synthetic-token-2")]

    @asynccontextmanager
    async def scope():  # type: ignore[no-untyped-def]
        yield object()

    bots = [
        SimpleNamespace(delete_webhook=AsyncMock(), session=SimpleNamespace(close=AsyncMock())) for _ in rows
    ]
    factory = Mock(side_effect=bots)
    monkeypatch.setattr(db_session, "session_scope", scope)
    monkeypatch.setattr(resale, "bots", AsyncMock(return_value=rows))
    monkeypatch.setattr(telegram, "make_bot", factory)
    monkeypatch.setattr(app, "set_commands", AsyncMock())
    result, fingerprint = await commands._reseller_bots()
    assert result == bots
    assert [call.kwargs["shop_id"] for call in factory.call_args_list] == [101, 202]
    assert fingerprint == frozenset((shop.id, sha256_hex(token)) for shop, token in rows)
