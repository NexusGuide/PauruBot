"""Error-report delivery survives Telegram failure and Redis/DB handoff interruptions."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramServerError
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from paurubot import kv
from paurubot.db.base import utcnow
from paurubot.db.models import Job, JobStatus, LogChannel
from paurubot.logs import TelegramErrorHandler, log_tags
from paurubot.notifications import error_reports, telegram
from paurubot.worker import jobs, queue
from paurubot.worker.runner import QUEUES, Retry, run_job
from tests import factories

pytestmark = pytest.mark.security


@pytest.fixture
async def setup(session, monkeypatch):  # type: ignore[no-untyped-def]
    await session.execute(update(Job).values(status=JobStatus.CANCELED))
    shop = await factories.shop(session, "Error delivery tenant")
    channel = LogChannel(shop_id=shop.id, topic="errors", chat_id=-100444)
    session.add(channel)
    await session.commit()
    monkeypatch.setattr(
        error_reports, "get_settings", lambda: SimpleNamespace(owner_telegram_ids=[990001, 990002])
    )
    send = AsyncMock(return_value=None)
    monkeypatch.setattr(telegram, "send_text", send)
    return shop, channel, send


async def _put(shop_id: int | None = None, message: str = "original application failure") -> None:
    await kv.get_redis().rpush(TelegramErrorHandler.KEY, json.dumps({"shop_id": shop_id, "msg": message}))


async def _rows(session):  # type: ignore[no-untyped-def]
    return list(
        await session.scalars(
            select(Job)
            .where(Job.kind == "error_report")
            .order_by(Job.id)
            .execution_options(populate_existing=True)
        )
    )


async def _run_due(session):  # type: ignore[no-untyped-def]
    claims = await queue.claim(session, "notify", limit=20)
    for claim in claims:
        await run_job("notify", QUEUES["notify"], claim)
    return claims


async def test_flush_does_not_call_telegram_and_creates_durable_job(session, setup) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    await jobs.flush_errors()
    send.assert_not_awaited()
    rows = await _rows(session)
    assert len(rows) == 1 and rows[0].shop_id == shop.id
    assert rows[0].status == JobStatus.QUEUED
    assert await kv.get_redis().get(error_reports.BATCH_KEY) is None
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 0
    await _run_due(session)
    assert (await _rows(session))[0].status == JobStatus.DONE
    send.assert_awaited_once()


async def test_database_failure_keeps_stable_batch_for_recovery(session, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    original = error_reports.session_scope

    @asynccontextmanager
    async def offline():  # type: ignore[no-untyped-def]
        raise RuntimeError("synthetic database outage")
        yield  # pragma: no cover

    monkeypatch.setattr(error_reports, "session_scope", offline)
    with pytest.raises(RuntimeError, match="outage"):
        await jobs.flush_errors()
    batch = await kv.get_redis().get(error_reports.BATCH_KEY)
    assert batch and "original application failure" in batch
    send.assert_not_awaited()
    monkeypatch.setattr(error_reports, "session_scope", original)
    await jobs.flush_errors()
    assert len(await _rows(session)) == 1
    await _run_due(session)
    send.assert_awaited_once()


async def test_ack_failure_after_commit_replays_enqueue_not_delivery(session, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    redis = kv.get_redis()
    original = redis.eval

    async def fail_ack(script, *args):  # type: ignore[no-untyped-def]
        if script == error_reports._ACK:
            raise ConnectionError("synthetic ack interruption")
        return await original(script, *args)

    monkeypatch.setattr(redis, "eval", fail_ack)
    with pytest.raises(ConnectionError):
        await jobs.flush_errors()
    assert len(await _rows(session)) == 1
    await _run_due(session)  # delivery may even complete before Redis is available again
    send.assert_awaited_once()
    monkeypatch.setattr(redis, "eval", original)
    await jobs.flush_errors()
    await _run_due(session)
    assert len(await _rows(session)) == 1
    send.assert_awaited_once()


async def test_compare_ack_cannot_delete_newer_batch(setup) -> None:  # type: ignore[no-untyped-def]
    redis = kv.get_redis()
    await redis.set(error_reports.BATCH_KEY, "newer")
    assert await redis.eval(error_reports._ACK, 1, error_reports.BATCH_KEY, "old") == 0
    assert await redis.get(error_reports.BATCH_KEY) == "newer"


async def test_batches_are_bounded_and_remaining_entries_are_not_lost(session, setup) -> None:  # type: ignore[no-untyped-def]
    shop, _, _ = setup
    for i in range(55):
        await _put(shop.id, f"failure {i}")
    await jobs.flush_errors()
    assert len(await _rows(session)) == 50
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 5
    await jobs.flush_errors()
    assert len(await _rows(session)) == 55


@pytest.mark.parametrize("unscoped", [False, True])
async def test_transient_delivery_failure_retries_without_periodic_recursion(
    session, setup, unscoped
) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(None if unscoped else shop.id)
    await jobs.flush_errors()
    send.side_effect = (
        [Retry("telegram unavailable: TelegramServerError"), None]
        if unscoped
        else Retry("telegram unavailable: TelegramServerError")
    )
    await _run_due(session)
    rows = await _rows(session)
    assert rows[0].status == JobStatus.QUEUED and rows[0].last_error
    if unscoped:
        assert rows[1].status == JobStatus.DONE  # other owner is isolated from first's failure
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 0
    assert await kv.get_redis().get(error_reports.BATCH_KEY) is None
    send.side_effect = None
    await session.execute(
        update(Job).where(Job.id == rows[0].id).values(run_at=utcnow() - timedelta(seconds=1))
    )
    await session.commit()
    await _run_due(session)
    assert all(row.status == JobStatus.DONE for row in await _rows(session))
    assert send.await_count == (3 if unscoped else 2)


async def test_retry_after_is_carried_through_delivery_and_worker_schedule(
    session, setup, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    shop, _, _ = setup
    await _put(shop.id)
    await jobs.flush_errors()
    method = SendMessage(chat_id=-100444, text="x")

    async def limited(*args, **kwargs):  # type: ignore[no-untyped-def]
        call = AsyncMock(side_effect=TelegramRetryAfter(method=method, message="429", retry_after=90))
        return await telegram.deliver(call)

    monkeypatch.setattr(telegram, "send_text", limited)
    monkeypatch.setattr(queue, "backoff", lambda _attempt: 1.0)
    before = utcnow()
    await _run_due(session)
    row = (await _rows(session))[0]
    assert row.status == JobStatus.QUEUED
    assert row.run_at >= before + timedelta(seconds=90)
    assert row.attempts == 1 and "90" in row.last_error


@pytest.mark.parametrize(
    "error",
    [
        telegram.Undeliverable("blocked"),
        TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="bad topic"),
    ],
)
async def test_permanent_failure_has_dead_row_not_recursive_alert(session, setup, error) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    await jobs.flush_errors()
    send.side_effect = error
    await _run_due(session)
    row = (await _rows(session))[0]
    assert row.status == JobStatus.DEAD and row.attempts == 1
    assert not list(await session.scalars(select(Job).where(Job.kind == "job_dead")))
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 0


async def test_exhausted_retries_do_not_enqueue_alert_about_alert(session, setup) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    await jobs.flush_errors()
    await session.execute(update(Job).where(Job.kind == "error_report").values(max_attempts=1))
    await session.commit()
    send.side_effect = Retry("synthetic server error")
    await _run_due(session)
    assert (await _rows(session))[0].status == JobStatus.DEAD
    assert not list(await session.scalars(select(Job).where(Job.kind == "job_dead")))


@pytest.mark.parametrize("unscoped", [False, True])
async def test_destination_revoked_before_delivery_is_not_rerouted(
    session, setup, monkeypatch, unscoped
) -> None:  # type: ignore[no-untyped-def]
    shop, channel, send = setup
    await _put(None if unscoped else shop.id)
    await jobs.flush_errors()
    if unscoped:
        monkeypatch.setattr(error_reports, "get_settings", lambda: SimpleNamespace(owner_telegram_ids=[]))
    else:
        channel.chat_id = -100999
        await session.commit()
    await _run_due(session)
    send.assert_not_awaited()
    assert all(row.status == JobStatus.DEAD for row in await _rows(session))


async def test_unexpected_error_report_job_failure_does_not_reenter_telegram_sink(session, setup) -> None:  # type: ignore[no-untyped-def]
    shop, _, send = setup
    await _put(shop.id)
    await jobs.flush_errors()
    send.side_effect = RuntimeError("synthetic bug in alert delivery")
    sink = TelegramErrorHandler("worker")
    logger = logging.getLogger("paurubot.worker.runner")
    logger.addHandler(sink)
    try:
        await _run_due(session)
    finally:
        logger.removeHandler(sink)
        if sink._client:
            sink._client.close()
    assert (await _rows(session))[0].status == JobStatus.QUEUED
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 0
    # Unrelated worker errors must still enter the operational error sink.
    token = log_tags.set({"queue": "provision", "kind": "create", "shop": shop.id})
    try:
        sink.handle(
            logging.makeLogRecord(
                {"name": logger.name, "msg": "real unrelated error", "levelno": logging.ERROR}
            )
        )
    finally:
        log_tags.reset(token)
        if sink._client:
            sink._client.close()
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 1


async def test_telegram_server_failure_is_classified_as_retry() -> None:
    call = AsyncMock(side_effect=TelegramServerError(method=SendMessage(chat_id=1, text="x"), message="502"))
    with pytest.raises(Retry, match="TelegramServerError") as caught:
        await telegram.deliver(call)
    assert caught.value.retry_after is None


@pytest.mark.parametrize("delay", [-1.0, float("inf"), float("nan")])
def test_retry_delay_validation(delay: float) -> None:
    with pytest.raises(ValueError):
        Retry("invalid", retry_after=delay)


async def test_transaction_rollback_replays_the_same_redis_batch(session, setup, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    shop, _, _ = setup
    await _put(shop.id)
    original = queue.enqueue

    async def interrupted(*args, **kwargs):  # type: ignore[no-untyped-def]
        await original(*args, **kwargs)
        raise RuntimeError("synthetic interruption before commit")

    monkeypatch.setattr(queue, "enqueue", interrupted)
    with pytest.raises(RuntimeError, match="before commit"):
        await jobs.flush_errors()
    assert not await _rows(session)
    assert await kv.get_redis().get(error_reports.BATCH_KEY)
    monkeypatch.setattr(queue, "enqueue", original)
    await jobs.flush_errors()
    assert len(await _rows(session)) == 1


async def test_competing_takers_see_one_stable_batch(setup) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    redis = kv.get_redis()
    for i in range(55):
        await _put(message=f"failure {i}")
    results = await asyncio.gather(
        *[
            redis.eval(
                error_reports._TAKE, 2, TelegramErrorHandler.KEY, error_reports.BATCH_KEY, f"candidate-{i}"
            )
            for i in range(10)
        ]
    )
    assert len(set(results)) == 1
    assert len(json.loads(results[0])["rows"]) == 50
    assert await redis.llen(TelegramErrorHandler.KEY) == 5


async def test_retry_floor_never_shortens_backoff_and_stale_claim_cannot_reschedule(
    session, setup, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from dataclasses import replace

    shop, _, _ = setup
    await _put(shop.id)
    await jobs.flush_errors()
    claimed = (await queue.claim(session, "notify", limit=1))[0]
    monkeypatch.setattr(queue, "backoff", lambda _attempt: 120.0)
    before = utcnow()
    assert await queue.fail(session, replace(claimed, token="stale"), "stale", retry_after=999) is None
    assert await queue.fail(session, claimed, "rate", retry_after=90) is False
    await session.commit()
    row = (await _rows(session))[0]
    assert row.run_at >= before + timedelta(seconds=120)
    assert row.last_error == "rate" and row.attempts == 1
