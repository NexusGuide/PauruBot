"""F-03: a re-delivered Telegram update runs its handler once; synthetic id 0 always runs."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Chat, Message, Update, User

from paurubot import kv
from paurubot.bot.dedupe import UpdateDedupeMiddleware


@pytest.fixture
def memory_once(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    seen: set[str] = set()

    async def set_once(key: str, ttl: int) -> bool:
        if key in seen:
            return False
        seen.add(key)
        return True

    async def forget_once(key: str) -> None:
        seen.discard(key)

    monkeypatch.setattr(kv, "set_once", set_once)
    monkeypatch.setattr(kv, "forget_once", forget_once)
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


async def test_failed_update_can_be_retried(memory_once: set[str]) -> None:
    mw = UpdateDedupeMiddleware()
    calls: list[int] = []
    with pytest.raises(RuntimeError):
        await _run(mw, _update(11), calls, fail=True)
    assert await _run(mw, _update(11), calls) == "ok"
    assert calls == [11, 11]
