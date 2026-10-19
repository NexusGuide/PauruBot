"""Operational errors must never fan out to unrelated shop log channels."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paurubot import kv
from paurubot.db.models import LogChannel
from paurubot.logs import TelegramErrorHandler, correlation_id, error_shop_id, log_tags
from paurubot.notifications import handlers, telegram
from paurubot.worker import jobs, queue
from tests import factories

pytestmark = pytest.mark.security
OWNER = 990001


@pytest.fixture
async def destinations(session, monkeypatch):  # type: ignore[no-untyped-def]
    first = await factories.shop(session, "First error tenant")
    second = await factories.shop(session, "Second error tenant")
    session.add_all(
        [
            LogChannel(shop_id=first.id, topic="errors", chat_id=-100101),
            LogChannel(shop_id=second.id, topic="all", chat_id=-100202),
        ]
    )
    await session.commit()
    send = AsyncMock(return_value=None)
    monkeypatch.setattr(telegram, "send_text", send)
    monkeypatch.setattr(handlers, "get_settings", lambda: SimpleNamespace(owner_telegram_ids=[OWNER]))
    return first, second, send


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "1", 1.0, [], {}])
def test_error_scope_never_coerces_untrusted_values(value: object) -> None:
    assert error_shop_id(value) is None
    assert error_shop_id(123) == 123


async def test_error_envelope_carries_only_task_scope_and_redacts_before_queueing() -> None:
    handler = TelegramErrorHandler("worker")
    tags_token = log_tags.set({"shop": 123, "customer": 456})
    cid_token = correlation_id.set("request-123")
    try:
        record = logging.makeLogRecord(
            {"name": "paurubot.test", "msg": "failed password=synthetic-secret", "levelno": logging.ERROR}
        )
        # Already-formatted traceback text must be scrubbed too, even without exc_info.
        record.exc_text = "RuntimeError: token=synthetic-exception-secret"
        handler.handle(record)
    finally:
        log_tags.reset(tags_token)
        correlation_id.reset(cid_token)
        if handler._client is not None:
            handler._client.close()
    raw = await kv.get_redis().lpop(TelegramErrorHandler.KEY)
    entry = json.loads(raw)
    assert entry["shop_id"] == 123 and entry["cid"] == "request-123"
    assert "synthetic-secret" not in raw and "synthetic-exception-secret" not in raw
    assert "customer" not in entry
    # Context does not bleed into the next task/record; a record's extra shop is not provenance.
    other = TelegramErrorHandler("api")
    try:
        other.handle(logging.makeLogRecord({"name": "paurubot.test", "msg": "unscoped", "shop": 123}))
    finally:
        if other._client is not None:
            other._client.close()
    assert json.loads(await kv.get_redis().lpop(TelegramErrorHandler.KEY))["shop_id"] is None


async def test_identical_errors_are_grouped_only_within_their_shop(destinations) -> None:  # type: ignore[no-untyped-def]
    first, second, send = destinations
    for scope, detail in [(first.id, "first-only"), (first.id, "first-again"), (second.id, "second-only")]:
        await kv.get_redis().rpush(
            TelegramErrorHandler.KEY,
            json.dumps({"shop_id": scope, "logger": "same", "msg": "same", "exc": detail}),
        )
    await jobs.flush_errors()
    assert send.await_count == 2
    calls = {call.args[0]: call for call in send.await_args_list}
    assert set(calls) == {-100101, -100202}
    assert "first-only" in calls[-100101].args[1] and "×2" in calls[-100101].args[1]
    assert "second-only" not in calls[-100101].args[1]
    assert "second-only" in calls[-100202].args[1] and "first-only" not in calls[-100202].args[1]
    assert calls[-100101].kwargs["shop_id"] == first.id
    assert calls[-100202].kwargs["shop_id"] == second.id


@pytest.mark.parametrize("scope", [None, True, "1", -1, 999999999])
async def test_legacy_invalid_and_deleted_scopes_are_platform_only(destinations, scope) -> None:  # type: ignore[no-untyped-def]
    first, _, send = destinations
    entry = {"logger": "legacy", "msg": "password=old-secret", "exc": "token=old-trace-secret"}
    if scope is not None:
        entry["shop_id"] = scope
    # Old/extra metadata must not be interpreted as a tenant fallback.
    entry["shop"] = first.id
    await kv.get_redis().rpush(TelegramErrorHandler.KEY, json.dumps(entry))
    await jobs.flush_errors()
    send.assert_awaited_once()
    call = send.await_args
    assert call.args[0] == OWNER and call.kwargs["shop_id"] is None
    assert "old-secret" not in call.args[1] and "old-trace-secret" not in call.args[1]


async def test_malformed_queue_records_do_not_crash_flush_or_route_to_shops(destinations) -> None:  # type: ignore[no-untyped-def]
    _, _, send = destinations
    await kv.get_redis().rpush(TelegramErrorHandler.KEY, "not-json", "null", "[]", '"text"', '{"msg": []}')
    await jobs.flush_errors()
    send.assert_not_awaited()


async def test_no_platform_owners_means_no_fallback_to_shop_channels(destinations, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, _, send = destinations
    monkeypatch.setattr(handlers, "get_settings", lambda: SimpleNamespace(owner_telegram_ids=[]))
    await kv.get_redis().rpush(TelegramErrorHandler.KEY, json.dumps({"msg": "global"}))
    await jobs.flush_errors()
    send.assert_not_awaited()


@pytest.mark.parametrize("scoped", [True, False])
async def test_dead_letter_alert_uses_the_same_tenant_boundary(session, destinations, scoped) -> None:  # type: ignore[no-untyped-def]
    first, _, send = destinations
    await queue.enqueue(
        session,
        "notify",
        "job_dead",
        {"job_id": 42, "queue": "ops", "kind": "backup", "error": "password=job-secret"},
        shop_id=first.id if scoped else None,
    )
    await session.commit()
    claimed = await queue.claim(session, "notify", limit=1)
    assert len(claimed) == 1
    await handlers.job_dead(session, claimed[0])
    send.assert_awaited_once()
    call = send.await_args
    assert call.args[0] == (-100101 if scoped else OWNER)
    assert call.kwargs["shop_id"] == (first.id if scoped else None)
    assert "job-secret" not in call.args[1]


async def test_platform_delivery_failure_does_not_loop_or_stop_other_owners(session, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        handlers, "get_settings", lambda: SimpleNamespace(owner_telegram_ids=[OWNER, OWNER + 1, OWNER])
    )
    send = AsyncMock(side_effect=[telegram.Undeliverable("blocked"), None])
    monkeypatch.setattr(telegram, "send_text", send)
    await handlers.report_error(session, None, "platform only")
    assert [call.args[0] for call in send.await_args_list] == [OWNER, OWNER + 1]
    assert await kv.get_redis().llen(TelegramErrorHandler.KEY) == 0
