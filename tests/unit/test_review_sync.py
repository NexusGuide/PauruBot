"""F-24: once one reviewer decides, every other copy of the receipt shows it and loses its buttons."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.methods import EditMessageCaption, EditMessageText

from paurubot import kv
from paurubot.notifications import review_sync, telegram
from tests.fakes.telegram import make_bot


class _Pipe:
    def __init__(self, store: dict[str, list[str]]) -> None:
        self.store = store
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> _Pipe:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        def queue_op(*args: Any) -> None:
            self.ops.append((name, args))

        return queue_op

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, args in self.ops:
            key = args[0]
            items = self.store.setdefault(key, [])
            if op == "rpush":
                items.extend(args[1:])
                out.append(len(items))
            elif op == "ltrim":
                self.store[key] = items[args[1] :] if args[1] < 0 else items
                out.append(True)
            elif op == "lrange":
                out.append(list(items))
            elif op == "delete":
                out.append(int(bool(self.store.pop(key, None))))
            else:  # expire
                out.append(True)
        return out


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, list[str]] = {}

    def pipeline(self, transaction: bool = True) -> _Pipe:
        return _Pipe(self.store)


@pytest.fixture
def fake() -> Iterator[Any]:
    redis = FakeRedis()
    kv.set_redis(redis)  # type: ignore[arg-type]
    bot, session = make_bot()
    telegram.set_bot(bot)
    yield session
    telegram.set_bot(None)
    kv.set_redis(None)


def _msg(chat: int, message_id: int, photo: bool = False) -> Any:
    return SimpleNamespace(chat=SimpleNamespace(id=chat), message_id=message_id, photo=[1] if photo else None)


async def test_remember_and_take_once(fake: Any) -> None:
    await review_sync.remember(7, _msg(100, 1, photo=True))
    await review_sync.remember(7, _msg(200, 2))
    await review_sync.remember(7, _msg(200, 2))  # the same copy twice is edited once
    await review_sync.remember(7, None)  # nothing was sent (guest chat, stopped bot)
    refs = await review_sync.take(7)
    assert refs == [{"chat": 100, "msg": 1, "photo": True}, {"chat": 200, "msg": 2, "photo": False}]
    assert await review_sync.take(7) == []  # a second sync finds nothing left to edit


async def test_edit_all_updates_every_other_copy(fake: Any) -> None:
    refs = [{"chat": 100, "msg": 1, "photo": True}, {"chat": 200, "msg": 2, "photo": False}]
    refs.append({"chat": 300, "msg": 3, "photo": False})
    left = await review_sync.edit_all(None, refs, "receipt\n\n✅ by Sara", skip=(300, 3))
    assert left == []
    captions = [c for c in fake.calls if isinstance(c, EditMessageCaption)]
    texts = [c for c in fake.calls if isinstance(c, EditMessageText)]
    assert [(c.chat_id, c.message_id) for c in captions] == [(100, 1)]
    assert [(c.chat_id, c.message_id) for c in texts] == [(200, 2)]  # the tapped copy is left alone
    assert all(c.reply_markup is None for c in captions + texts)
    assert "Sara" in (texts[0].text or "")


def test_label_prefers_display_name() -> None:
    assert review_sync.label(SimpleNamespace(id=4, display_name="Sara", username="s")) == "Sara"
    assert review_sync.label(SimpleNamespace(id=4, display_name=None, username="s")) == "s"
    assert review_sync.label(SimpleNamespace(id=4, display_name=None, username=None)) == "#4"
