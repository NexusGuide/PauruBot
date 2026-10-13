"""F-03: only successful updates are remembered; in-flight work must remain retryable."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Chat, Message, Update, User

from paurubot import kv
from paurubot.bot import dedupe
from paurubot.bot.dedupe import UpdateDedupeMiddleware


@pytest.fixture
def memory_once(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    seen: set[str] = set()
    locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def lock(key: str, ttl: int = 30, **kwargs: Any) -> AsyncIterator[str]:
        assert ttl > dedupe.HANDLER_TIMEOUT_SECONDS
        async with locks.setdefault(key, asyncio.Lock()):
            yield "owner"

    async def seen_once(key: str) -> bool:
        return key in seen

    async def complete_once(key: str, owner: str, ttl: int) -> None:
        assert owner == "owner" and ttl == dedupe.TTL_SECONDS
        seen.add(key)

    monkeypatch.setattr(kv, "lock", lock)
    monkeypatch.setattr(kv, "seen_once", seen_once)
    monkeypatch.setattr(kv, "complete_once", complete_once)
    return seen


def _update(update_id: int) -> Update:
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=5, type="private"),
        from_user=User(id=5, is_bot=False, first_name="A"),
        text="hi",
    )
    return Update(update_id=update_id, message=msg)


async def _run(mw: UpdateDedupeMiddleware, update: Update, calls: list[int], fail: bool = False) -> Any:
    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append(event.update_id)
        if fail:
            raise RuntimeError("boom")
        return "ok"

    return await mw(handler, update, {"bot": SimpleNamespace(id=99)})


async def test_duplicate_update_runs_once(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()
    calls: list[int] = []
    assert await _run(mw, _update(10), calls) == "ok"
    assert await _run(mw, _update(10), calls) is None
    assert calls == [10]


async def test_synthetic_update_zero_is_never_deduped(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()
    calls: list[int] = []
    await _run(mw, _update(0), calls)
    await _run(mw, _update(0), calls)
    assert calls == [0, 0]
    assert not memory_once


async def test_failed_update_can_be_retried(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()
    calls: list[int] = []
    with pytest.raises(RuntimeError, match="boom"):
        await _run(mw, _update(11), calls, fail=True)
    assert not memory_once
    assert await _run(mw, _update(11), calls) == "ok"
    assert calls == [11, 11]


async def test_cancelled_update_can_be_retried(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()

    async def handler(event: Any, data: dict[str, Any]) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await mw(handler, _update(12), {"bot": SimpleNamespace(id=99)})
    assert not memory_once
    assert await _run(mw, _update(12), []) == "ok"


async def test_concurrent_duplicate_waits_for_success(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()
    started, finish = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def handler(event: Any, data: dict[str, Any]) -> str:
        calls.append(event.update_id)
        started.set()
        await finish.wait()
        return "ok"

    first = asyncio.create_task(mw(handler, _update(13), {"bot": SimpleNamespace(id=99)}))
    await started.wait()
    assert not memory_once  # a crash here must not leave a 24-hour completed marker
    second = asyncio.create_task(mw(handler, _update(13), {"bot": SimpleNamespace(id=99)}))
    try:
        await asyncio.sleep(0)
        assert not second.done()  # no early successful ACK of unfinished work
    finally:
        finish.set()
        result = await asyncio.gather(first, second)
    assert result == ["ok", None] and calls == [13]


async def test_same_update_id_in_different_bots_is_independent(memory_once: set[str]) -> None:
    calls: list[int] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        calls.append(data["bot"].id)

    for bot_id in [1, 2]:
        await UpdateDedupeMiddleware()(handler, _update(14), {"bot": SimpleNamespace(id=bot_id)})
    assert calls == [1, 2]


async def test_busy_lease_is_not_acknowledged(memory_once: set[str], monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def busy(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
        raise kv.LockNotAcquired("busy")
        yield "never"  # pragma: no cover

    monkeypatch.setattr(kv, "lock", busy)
    calls: list[int] = []
    with pytest.raises(kv.LockNotAcquired):
        await _run(UpdateDedupeMiddleware(), _update(15), calls)
    assert not calls and not memory_once


async def test_redis_unavailable_before_work_is_fail_open(
    memory_once: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(key: str) -> bool:
        raise ConnectionError("redis down")

    monkeypatch.setattr(kv, "seen_once", unavailable)
    calls: list[int] = []
    assert await _run(UpdateDedupeMiddleware(), _update(16), calls) == "ok"
    assert calls == [16]


async def test_completion_failure_does_not_run_handler_twice(
    memory_once: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(*args: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(kv, "complete_once", unavailable)
    calls: list[int] = []
    with pytest.raises(ConnectionError):
        await _run(UpdateDedupeMiddleware(), _update(17), calls)
    assert calls == [17]


async def test_handler_timeout_leaves_update_retryable(
    memory_once: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dedupe, "HANDLER_TIMEOUT_SECONDS", 0.01)

    async def stuck(event: Any, data: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError):
        await UpdateDedupeMiddleware()(stuck, _update(18), {"bot": SimpleNamespace(id=99)})
    assert not memory_once
    assert await _run(UpdateDedupeMiddleware(), _update(18), []) == "ok"


async def test_cancelled_inflight_task_releases_lease(memory_once: set[str]) -> None:
    started = asyncio.Event()

    async def handler(event: Any, data: dict[str, Any]) -> None:
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        UpdateDedupeMiddleware()(handler, _update(19), {"bot": SimpleNamespace(id=99)})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not memory_once
    assert await _run(UpdateDedupeMiddleware(), _update(19), []) == "ok"


async def test_complete_once_checks_lease_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    monkeypatch.setattr(kv, "get_redis", lambda: redis)
    await kv.complete_once("upd:1:2", "owner", 3600)
    args = redis.eval.call_args.args
    assert args[1:] == (2, "lock:upd:1:2", "once:upd:1:2", "owner", 3600)


async def test_complete_once_rejects_lost_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    redis = SimpleNamespace(eval=AsyncMock(return_value=0))
    monkeypatch.setattr(kv, "get_redis", lambda: redis)
    with pytest.raises(kv.LockNotAcquired):
        await kv.complete_once("upd:1:2", "old-owner", 3600)
